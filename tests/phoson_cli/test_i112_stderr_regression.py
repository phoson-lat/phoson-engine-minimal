"""I-112 regression: soft-fail warnings must not leak to stderr.

The issue: a soft-fail that emits ``warnings.warn(...)`` (context-window /
model-listing fallbacks) produced *two* outputs in classic / one-shot mode —
the intended styled notice *and* a raw Python ``UserWarning`` with source
file + line on stderr. After the I-112 hook these same paths must write
**nothing** to stderr and surface the warning exactly once, as a styled
notice (no internal file paths / code lines).

The exact issue repro is the vLLM "server is up but the model id is not
listed" fallback (``_resolve_vllm`` else-branch).
"""

import sys
import types
import contextlib
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import phoson_cli.__main__ as main_module
import phoson_cli.warnings_hook as wh
from phoson_cli.config import PhosonConfig
from phoson_agent.plugins.context_window import (
    DEFAULT_CONTEXT_WINDOW,
    ContextWindowResolver,
)

# ── Reusable fake httpx (mirrors test_context_window._FakeVLLMClient) ─────────


class _FakeVLLMResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeVLLMClient:
    def __init__(self, payload: dict | None, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code

    async def get(self, url: str, **kwargs: object) -> _FakeVLLMResponse:
        if self._payload is None:
            raise httpx.ConnectError("connection refused")
        return _FakeVLLMResponse(self._payload, self._status_code)

    async def __aenter__(self) -> "_FakeVLLMClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_vllm_httpx(monkeypatch: pytest.MonkeyPatch, client: _FakeVLLMClient) -> None:
    import phoson_agent.plugins.context_window as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: client)


def _vllm_payload(model_id: str, max_model_len: int) -> dict:
    return {
        "object": "list",
        "data": [{"id": model_id, "max_model_len": max_model_len}],
    }


@contextlib.contextmanager
def _hooked():
    """Run the body with the hook installed and the plain default printer,
    always restoring both afterwards (safe around ``SystemExit``)."""
    restore = wh.install()
    wh.reset_notice_printer()
    try:
        yield
    finally:
        restore()
        wh.reset_notice_printer()


@pytest.fixture(autouse=True)
def _reset_hook_state():
    """Keep module-level hook state clean across tests."""
    wh.set_fullscreen_active(False)
    wh.reset_notice_printer()
    yield
    wh.set_fullscreen_active(False)
    wh.reset_notice_printer()


# ── 7. The exact issue repro: vLLM model not listed → nothing on stderr ──────


@pytest.mark.asyncio
async def test_vllm_model_not_listed_writes_nothing_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _FakeVLLMClient(_vllm_payload("OTHER", 4096))
    _patch_vllm_httpx(monkeypatch, client)
    resolver = ContextWindowResolver(vllm_base_url="http://localhost:8383/v1")

    with _hooked():
        result = await resolver.resolve("vllm", "google/gemini-2.5-flash")

    out, err = capsys.readouterr()
    assert result == DEFAULT_CONTEXT_WINDOW
    # The whole point of I-112: the raw UserWarning never reaches stderr.
    assert err == ""
    # ... and it is surfaced exactly once, as a styled notice on stdout.
    assert "vLLM /v1/models response did not include" in out
    assert out.count("vLLM /v1/models response did not include") == 1
    # No internal file paths or code lines leak to the user.
    assert ".py:" not in out
    assert ".py:" not in err


# ── 8. Server down (except branch) → single notice, no double, no stderr ────


@pytest.mark.asyncio
async def test_vllm_unreachable_writes_single_notice_no_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _FakeVLLMClient(None)  # raises on get()
    _patch_vllm_httpx(monkeypatch, client)
    resolver = ContextWindowResolver(vllm_base_url="http://127.0.0.1:9/v1")

    with _hooked():
        result = await resolver.resolve("vllm", "m1")

    out, err = capsys.readouterr()
    assert result == DEFAULT_CONTEXT_WINDOW
    assert err == ""
    # Dedup guarantee: the except branch logs once (issue-#23 trace) and no
    # longer also warns → exactly ONE notice, not two.
    assert "vLLM context window lookup failed" in out
    assert out.count("vLLM context window lookup failed") == 1
    assert ".py:" not in out


# ── 9. Classic model-listing fallback → notice, no stderr, I-113 intact ─────


@pytest.mark.asyncio
async def test_ollama_listing_failure_no_stderr_and_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="ollama",
        model="llama3.2",
        ollama_base_url="http://localhost:11434",
    )

    class DummyClient:
        async def __aenter__(self):
            raise httpx.ConnectError("boom")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient", lambda timeout: DummyClient()
    )

    from phoson_cli.model_selector import list_available_models

    with _hooked():
        models = await list_available_models(config)

    out, err = capsys.readouterr()
    # I-113 fallback still works: the current model is retained.
    assert [m.id for m in models] == ["llama3.2"]
    # The listing-failure warning is a notice now, never raw stderr.
    assert err == ""
    assert "Failed to fetch Ollama models" in out
    assert out.count("Failed to fetch Ollama models") == 1
    assert ".py:" not in out


# ── 10. One-shot mode: a mid-run warning → notice on stdout, stderr empty ───


class _FakeChat:
    async def aclose(self) -> None:
        pass


class _WarnEngine:
    def __init__(self, **_kwargs) -> None:
        self.context = types.SimpleNamespace(extra={})
        self.tools = []
        self._loaded_plugins = []

    async def run(self, messages, config):  # noqa: ANN001
        import warnings

        warnings.warn(
            "vLLM /v1/models response did not include 'x/y' (simulated)",
            UserWarning,
            stacklevel=2,
        )
        return types.SimpleNamespace(final_content="ONE-SHOT RESULT")


@pytest.mark.asyncio
async def test_run_oneshot_warning_is_notice_not_stderr(capsys, tmp_path: Path) -> None:
    with (
        patch("phoson_cli.__main__.build_chat", return_value=_FakeChat()),
        patch("phoson_agent.AgentEngine", _WarnEngine),
        _hooked(),  # simulate main() having installed the hook
    ):
        rc = await main_module._run_oneshot(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
        )

    out, err = capsys.readouterr()
    assert rc == 0
    assert "ONE-SHOT RESULT" in out
    # Mid-run warning surfaces once as a notice, never as raw stderr.
    assert err == ""
    assert "vLLM /v1/models response did not include" in out
    assert ".py:" not in out


# ── main() wiring: the hook is active during the run, restored on exit ──────


def test_main_hook_active_during_run_and_restored_on_exit(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The hook must be installed *during* ``main()`` (before arg parsing)
    and fully restored once ``main`` returns — including on the ``sys.exit``
    path (``_fail`` → ``sys.exit(2)``), since ``SystemExit`` fires the
    wrapper's ``finally``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["phoson-cli", "--bogus"])

    import warnings

    seen = {}

    def _spy_fail(message: str) -> None:  # noqa: ARG001
        seen["showwarning"] = warnings.showwarning
        raise SystemExit(2)

    monkeypatch.setattr(main_module, "_fail", _spy_fail)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 2
    # Active during the run, restored after (the sys.exit path ran finally).
    assert seen["showwarning"] is wh._hooked_showwarning
    assert warnings.showwarning is not wh._hooked_showwarning


def test_warn_during_run_is_notice_not_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """With the hook installed, a UserWarning prints one notice to stdout and
    nothing to stderr — no file path / code line."""
    import warnings

    restore = wh.install()
    try:
        warnings.warn("context window lookup failed: refused")
    finally:
        restore()

    out, err = capsys.readouterr()
    assert err == ""
    assert "UserWarning: context window lookup failed: refused" in out
    assert ".py:" not in out
