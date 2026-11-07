"""web_fetch DNS-rebinding hardening: connections are pinned to validated IPs.

These tests drive the *real* ``httpx.AsyncClient`` + ``_PinnedTransport``.
No real network is used: DNS is ``web_fetch._getaddrinfo`` (monkeypatched)
and the TCP layer is httpcore's ``AutoBackend.connect_tcp`` (monkeypatched to
record the address it was asked to connect to and serve a canned response).
"""

import socket
import importlib

import httpx
import pytest
import httpcore
from httpcore._backends.auto import AutoBackend


def _mod():
    return importlib.import_module("phoson_cli.tools.web_fetch")


class _RecordingStream(httpcore.AsyncNetworkStream):
    """Serves canned bytes and records what the client wrote / TLS SNI."""

    def __init__(self, response: bytes, log: dict) -> None:
        self._chunks = [response]
        self._log = log

    async def read(self, max_bytes, timeout=None):
        return self._chunks.pop(0) if self._chunks else b""

    async def write(self, buffer, timeout=None):
        self._log.setdefault("written", b"")
        self._log["written"] += buffer

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._log.setdefault("sni", []).append(server_hostname)
        return self

    def get_extra_info(self, info):
        return None


def _ok(body: bytes = b"hello pinned") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


def _redirect(location: str) -> bytes:
    return (
        b"HTTP/1.1 302 Found\r\nLocation: " + location.encode() + b"\r\n"
        b"Content-Length: 0\r\n\r\n"
    )


@pytest.fixture
def fake_tcp(monkeypatch):
    """Replace the real socket connect; record every (host, port) dialled."""
    log: dict = {"connects": [], "responses": []}

    async def _connect_tcp(self, host, port, **kwargs):
        log["connects"].append((host, port))
        return _RecordingStream(log["responses"].pop(0), log)

    monkeypatch.setattr(AutoBackend, "connect_tcp", _connect_tcp)
    return log


def _dns(answers: dict[str, list[str]], calls: dict[str, int]):
    """Resolver returning ``answers[host][n]`` on the n-th lookup (last repeats)."""

    def _resolve(host, port, *a, **k):
        if host not in answers:
            raise socket.gaierror(f"Name or service not known: {host}")
        n = calls.get(host, 0)
        calls[host] = n + 1
        ip = answers[host][min(n, len(answers[host]) - 1)]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _resolve


# ── (1) DNS rebinding: public at validation, private at connect ─────────────


async def test_rebinding_resolver_cannot_reach_private_ip(monkeypatch, fake_tcp):
    mod = _mod()
    calls: dict[str, int] = {}
    # 1st lookup (pre-flight check) → public; every later lookup → loopback.
    monkeypatch.setattr(
        mod,
        "_getaddrinfo",
        _dns({"rebind.example": ["93.184.216.34", "127.0.0.1"]}, calls),
    )
    fake_tcp["responses"].append(_ok(b"secret internal data"))

    result = await mod.web_fetch.handler({"url": "http://rebind.example/"}, None)

    assert calls["rebind.example"] >= 2  # validated, then re-resolved
    assert fake_tcp["connects"] == []  # never dialled anything, let alone 127.0.0.1
    assert "Refusing to fetch rebind.example" in result
    assert "loopback" in result
    assert "secret internal data" not in result


async def test_connection_goes_to_validated_ip_with_hostname_sni_and_host(
    monkeypatch, fake_tcp
):
    mod = _mod()
    monkeypatch.setattr(
        mod, "_getaddrinfo", _dns({"pub.example": ["93.184.216.34"]}, {})
    )
    fake_tcp["responses"].append(_ok())

    result = await mod.web_fetch.handler({"url": "https://pub.example/doc"}, None)

    assert "hello pinned" in result
    # TCP went to the validated IP, not the hostname (no second resolution)...
    assert fake_tcp["connects"] == [("93.184.216.34", 443)]
    # ...while TLS SNI and the Host header keep the original hostname.
    assert fake_tcp["sni"] == ["pub.example"]
    assert b"Host: pub.example\r\n" in fake_tcp["written"]


async def test_any_private_address_in_answer_is_refused(monkeypatch, fake_tcp):
    """A mixed public+private answer at connect time is refused outright."""
    mod = _mod()

    def _resolve(host, port, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(mod, "_getaddrinfo", _resolve)
    with pytest.raises(httpcore.ConnectError, match="metadata"):
        await mod._PinnedBackend().connect_tcp("mixed.example", 80)
    assert fake_tcp["connects"] == []


# ── (2) redirects to private hosts are still blocked ───────────────────────


async def test_redirect_to_private_host_is_blocked(monkeypatch, fake_tcp):
    mod = _mod()
    monkeypatch.setattr(
        mod,
        "_getaddrinfo",
        _dns({"ok.example": ["93.184.216.34"], "evil.example": ["10.0.0.5"]}, {}),
    )
    fake_tcp["responses"].append(_redirect("http://evil.example/admin"))

    result = await mod.web_fetch.handler({"url": "http://ok.example/r"}, None)

    assert fake_tcp["connects"] == [("93.184.216.34", 80)]
    assert "Refusing to fetch evil.example" in result
    assert "10.0.0.5" in result


async def test_redirect_hop_rebinding_is_blocked(monkeypatch, fake_tcp):
    """The redirect target passes the per-hop guard, then rebinds at connect."""
    mod = _mod()
    monkeypatch.setattr(
        mod,
        "_getaddrinfo",
        _dns(
            {
                "ok.example": ["93.184.216.34"],
                "evil.example": ["93.184.216.35", "169.254.169.254"],
            },
            {},
        ),
    )
    fake_tcp["responses"].append(_redirect("http://evil.example/latest/meta-data/"))

    result = await mod.web_fetch.handler({"url": "http://ok.example/r"}, None)

    assert fake_tcp["connects"] == [("93.184.216.34", 80)]  # only the first hop
    assert "Refusing to fetch evil.example" in result
    assert "metadata" in result


async def test_redirect_to_literal_private_ip_is_blocked(monkeypatch, fake_tcp):
    mod = _mod()
    monkeypatch.setattr(
        mod, "_getaddrinfo", _dns({"ok.example": ["93.184.216.34"]}, {})
    )
    fake_tcp["responses"].append(_redirect("http://127.0.0.1:8080/"))

    result = await mod.web_fetch.handler({"url": "http://ok.example/r"}, None)

    assert fake_tcp["connects"] == [("93.184.216.34", 80)]
    assert "Refusing to fetch 127.0.0.1" in result


# ── (3) trust_env is off: proxy env vars cannot route around the check ─────


async def test_client_ignores_proxy_env(monkeypatch, fake_tcp):
    mod = _mod()
    monkeypatch.setenv("HTTP_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setattr(
        mod, "_getaddrinfo", _dns({"pub.example": ["93.184.216.34"]}, {})
    )

    seen: dict = {}
    real_client = httpx.AsyncClient

    class _SpyClient(real_client):
        def __init__(self, *a, **k):
            seen.update(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _SpyClient)
    fake_tcp["responses"].append(_ok())

    result = await mod.web_fetch.handler({"url": "http://pub.example/"}, None)

    assert seen["trust_env"] is False
    assert isinstance(seen["transport"], mod._PinnedTransport)
    assert fake_tcp["connects"] == [("93.184.216.34", 80)]  # not the proxy
    assert "hello pinned" in result
