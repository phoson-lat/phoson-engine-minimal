"""Tests for issue #183 (F-06) — web_fetch SSRF filter + download cap.

- Non-public hosts (metadata, loopback, RFC1918, CGNAT, link-local, ULA) are
  refused on the initial URL *and* on every redirect hop.
- The body is streamed with a hard byte cap, so a huge response is not fully
  buffered.
- Every result carries the untrusted-content prefix.
"""

import httpx
import pytest

from phoson_cli.tools.web_fetch import (
    MAX_DOWNLOAD_BYTES,
    _request_guard,
    _BlockedHostError,
    assert_public_url,
    format_fetch_result,
)


def _fetch_module():
    import importlib

    return importlib.import_module("phoson_cli.tools.web_fetch")


def _fake_dns(mapping: dict[str, str]):
    import socket

    def _resolve(host, port, *a, **k):
        if host in mapping:
            return [(2, 1, 6, "", (mapping[host], port))]
        raise socket.gaierror(f"Name or service not known: {host}")

    return _resolve


# ── assert_public_url: literal IPs and hostnames ────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8080/",  # loopback
        "http://10.0.0.5/internal",  # RFC1918
        "http://192.168.1.1/",  # RFC1918
        "http://172.16.0.1/",  # RFC1918
        "http://100.64.0.1/",  # CGNAT
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # IPv6 loopback
        "http://[fc00::1]/",  # IPv6 unique-local
        "http://[fe80::1]/",  # IPv6 link-local
    ],
)
def test_assert_public_url_refuses_non_public_ip(url) -> None:
    with pytest.raises(_BlockedHostError):
        assert_public_url(url)


def test_assert_public_url_refuses_metadata_by_message() -> None:
    with pytest.raises(_BlockedHostError, match="metadata"):
        assert_public_url("http://169.254.169.254/latest/meta-data/")


def test_assert_public_url_allows_public_ip() -> None:
    assert_public_url("https://93.184.216.34/")  # example.com
    assert_public_url("https://8.8.8.8/")


def test_assert_public_url_refuses_localhost_hostname() -> None:
    with pytest.raises(_BlockedHostError):
        assert_public_url("http://localhost:8080/")


def test_assert_public_url_refuses_hostname_resolving_to_private(
    monkeypatch,
) -> None:
    mod = _fetch_module()

    monkeypatch.setattr(
        mod, "_getaddrinfo", _fake_dns({"internal.example": "10.0.0.5"})
    )
    with pytest.raises(_BlockedHostError, match="resolves to"):
        assert_public_url("https://internal.example/")


def test_assert_public_url_allows_hostname_resolving_to_public(monkeypatch) -> None:
    mod = _fetch_module()

    monkeypatch.setattr(
        mod, "_getaddrinfo", _fake_dns({"public.example": "93.184.216.34"})
    )
    assert_public_url("https://public.example/")


def test_assert_public_url_lets_unresolvable_host_through(monkeypatch) -> None:
    """An NXDOMAIN host is not an SSRF hit; the fetch itself reports it."""
    mod = _fetch_module()

    monkeypatch.setattr(mod, "_getaddrinfo", _fake_dns({}))  # resolves nothing
    assert_public_url("https://no-such-host.example/")  # no raise


# ── _request_guard: the per-redirect hook ───────────────────────────────────


async def test_request_guard_refuses_private_redirect() -> None:
    request = httpx.Request("GET", "http://10.0.0.5/internal")
    # The async client awaits the hook (httpx/_client.py:
    # ``await hook(request)``) — so the guard must be a coroutine function.
    import inspect

    assert inspect.iscoroutinefunction(_request_guard)
    with pytest.raises(httpx.ConnectError, match="10\\.0\\.0\\.5"):
        await _request_guard(request)


async def test_request_guard_allows_public() -> None:
    request = httpx.Request("GET", "https://93.184.216.34/")
    await _request_guard(request)  # no raise


# ── web_fetch handler: end-to-end ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_fetch_public_url_happy_path_survives_hook_await(monkeypatch):
    """A public URL must not crash on the client's ``await hook(request)``.

    Regression: the SSRF guard was originally a *sync* function, so on the
    first (public) request the async client did ``await None`` and every
    ``web_fetch`` failed with ``object NoneType can't be used in 'await'
    expression``. This test runs the real happy path — guard allows,
    response streams — exactly the way httpx's async client drives it.
    """
    mod = _fetch_module()
    monkeypatch.setattr(
        mod, "_getaddrinfo", _fake_dns({"public.example": "93.184.216.34"})
    )

    class _FakeResponse:
        def raise_for_status(self):
            return None

        headers = {"content-type": "text/plain"}

        async def aiter_bytes(self):
            yield b"hello public"

    # Faithful to httpx/_client.py: the async client AWAITS the request hook
    # in __aenter__ of the stream context manager, on every hop.
    class _Client:
        def __init__(self, *a, **k):
            self.hooks = k.get("event_hooks", {}).get("request", [])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            hooks = self.hooks

            class _CM:
                async def __aenter__(self):
                    for hook in hooks:
                        await hook(httpx.Request("GET", url))
                    return _FakeResponse()

                async def __aexit__(self, *exc):
                    return False

            return _CM()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = await mod.web_fetch.handler({"url": "https://public.example/page"}, None)
    assert "Fetched https://public.example/page" in result
    assert "hello public" in result
    # The failure mode this guards against must be absent.
    assert "await" not in result


@pytest.mark.asyncio
async def test_web_fetch_refuses_metadata_endpoint_pre_connect(monkeypatch) -> None:
    """The metadata IP is refused *before* any client is created."""
    mod = _fetch_module()
    called = False

    class _ShouldNotBeUsed:
        def __init__(self, *a, **k):
            nonlocal called
            called = True

    monkeypatch.setattr(mod.httpx, "AsyncClient", _ShouldNotBeUsed)
    result = await mod.web_fetch.handler(
        {"url": "http://169.254.169.254/latest/meta-data/"}, None
    )
    assert "metadata" in result
    assert not called  # refused pre-flight, no connection attempt


@pytest.mark.asyncio
async def test_web_fetch_refuses_localhost(monkeypatch) -> None:
    mod = _fetch_module()

    class _ShouldNotBeUsed:
        def __init__(self, *a, **k):
            raise AssertionError("no client should be created")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _ShouldNotBeUsed)
    result = await mod.web_fetch.handler({"url": "http://localhost:8080/"}, None)
    assert "localhost" in result


@pytest.mark.asyncio
async def test_web_fetch_refuses_hostname_resolving_to_private(monkeypatch) -> None:
    mod = _fetch_module()
    monkeypatch.setattr(
        mod, "_getaddrinfo", _fake_dns({"internal.example": "172.16.5.9"})
    )

    class _ShouldNotBeUsed:
        def __init__(self, *a, **k):
            raise AssertionError("no client should be created")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _ShouldNotBeUsed)
    result = await mod.web_fetch.handler({"url": "https://internal.example/"}, None)
    assert "resolves to" in result


@pytest.mark.asyncio
async def test_web_fetch_refuses_redirect_to_private(monkeypatch) -> None:
    """A 302 that lands on a private address is refused, not followed."""
    mod = _fetch_module()
    monkeypatch.setattr(mod, "_getaddrinfo", _fake_dns({"ok.example": "93.184.216.34"}))

    class _GuardedStreamCM:
        """Async CM that mirrors httpx: the request hook fires in __aenter__."""

        def __init__(self, kw):
            self.kw = kw

        async def __aenter__(self):
            # Replicate the real async client: it AWAITS the request hook
            # on every hop (httpx/_client.py: ``await hook(request)``).
            # A sync guard would surface here as
            # "object NoneType can't be used in 'await' expression".
            redirect_request = httpx.Request("GET", "http://10.0.0.5/internal")
            for hook in self.kw.get("event_hooks", {}).get("request", []):
                await hook(redirect_request)  # must raise httpx.ConnectError
            raise AssertionError("guard should have raised before this")

        async def __aexit__(self, *exc):
            return False

    class _RedirectingClient:
        def __init__(self, *a, **k):
            self.kw = k

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            return _GuardedStreamCM(self.kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _RedirectingClient)
    result = await mod.web_fetch.handler({"url": "https://ok.example/redirect"}, None)
    # The guard's message surfaces via the generic HTTPError handler.
    assert "10.0.0.5" in result
    assert "Refusing to fetch" in result


@pytest.mark.asyncio
async def test_web_fetch_caps_huge_body_and_stops_early(monkeypatch) -> None:
    """A multi-MB body is not fully downloaded (read stops at the cap)."""
    mod = _fetch_module()
    monkeypatch.setattr(
        mod, "_getaddrinfo", _fake_dns({"big.example": "93.184.216.34"})
    )

    chunk = 1024 * 1024  # 1 MB
    total_chunks = 10  # 10 MB total — far above the 2 MB cap

    class _HugeResponse:
        def __init__(self):
            self.yielded = 0

        def raise_for_status(self) -> None: ...

        @property
        def headers(self):
            return {"content-type": "text/plain"}

        async def aiter_bytes(self):
            for _ in range(total_chunks):
                self.yielded += 1
                yield b"x" * chunk

    captured: dict = {}

    class _Client:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            resp = _HugeResponse()
            captured["resp"] = resp

            class _Ctx:
                async def __aenter__(self2):
                    return resp

                async def __aexit__(self2, *exc):
                    return False

            return _Ctx()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = await mod.web_fetch.handler({"url": "https://big.example/huge"}, None)

    # Only enough chunks to reach the cap were read (2 × 1 MB == 2 MB cap),
    # not all 10 — the 10 MB body was not buffered in full.
    assert captured["resp"].yielded == MAX_DOWNLOAD_BYTES // chunk
    assert "untrusted data" in result


# ── untrusted-content prefix on every result path ──────────────────────────


def test_format_fetch_result_always_has_untrusted_prefix() -> None:
    assert "untrusted data" in format_fetch_result("https://x", "text/plain", "body")
    assert "untrusted data" in format_fetch_result("https://x", "text/plain", "   ")
