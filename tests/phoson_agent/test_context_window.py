"""Tests for context_window resolver."""

import pytest

from phoson_agent.plugins.context_window import (
    DEFAULT_CONTEXT_WINDOW,
    CONTEXT_WINDOW_REGISTRY,
    ContextWindowResolver,
)

# ── Static registry tests ────────────────────────────────────────────


class TestContextWindowRegistry:
    def test_registry_has_anthropic_models(self):
        for key in CONTEXT_WINDOW_REGISTRY:
            if key.startswith("anthropic/"):
                assert CONTEXT_WINDOW_REGISTRY[key] == 200_000

    def test_registry_has_openai_models(self):
        for key in CONTEXT_WINDOW_REGISTRY:
            if key.startswith("openai/"):
                assert CONTEXT_WINDOW_REGISTRY[key] == 128_000


# ── Resolver: static lookup ─────────────────────────────────────────


@pytest.mark.asyncio
class TestResolverStatic:
    async def test_registry_hit(self):
        r = ContextWindowResolver()
        assert await r.resolve("anthropic", "claude-sonnet-4-6") == 200_000
        assert await r.resolve("openai", "gpt-4o") == 128_000

    async def test_anthropic_prefix_match(self):
        r = ContextWindowResolver()
        # Not in registry but starts with claude-
        assert await r.resolve("anthropic", "claude-5-sonnet") == 200_000

    async def test_openai_prefix_match(self):
        r = ContextWindowResolver()
        # Not in registry but starts with gpt-4
        assert await r.resolve("openai", "gpt-5") == 128_000
        assert await r.resolve("openai", "o3-pro") == 128_000

    async def test_fallback_default(self):
        r = ContextWindowResolver()
        assert await r.resolve("unknown", "mystery-model") == DEFAULT_CONTEXT_WINDOW


# ── Resolver: Ollama ────────────────────────────────────────────────


class TestOllamaNumCtxExtraction:
    def test_extract_from_params_string(self):
        data = {
            "parameters": "num_ctx 8192\nnum_gpu 1",
        }
        assert ContextWindowResolver._extract_ollama_num_ctx(data) == 8192

    def test_extract_from_params_dict(self):
        data = {
            "parameters": {"num_ctx": 4096, "temperature": 0.7},
        }
        assert ContextWindowResolver._extract_ollama_num_ctx(data) == 4096

    def test_extract_from_model_info(self):
        data = {
            "model_info": {
                "llama.context_length": 32768,
            },
        }
        assert ContextWindowResolver._extract_ollama_num_ctx(data) == 32768

    def test_returns_none_when_missing(self):
        data = {"details": {"parameter_size": "7B"}}
        assert ContextWindowResolver._extract_ollama_num_ctx(data) is None


# ── Resolver: vLLM ──────────────────────────────────────────────────


class _FakeVLLMResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeVLLMClient:
    """Mimics ``httpx.AsyncClient`` for ``GET /v1/models``."""

    def __init__(self, payload: dict | None, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code
        self.get_calls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> _FakeVLLMResponse:
        self.get_calls.append(url)
        if self._payload is None:
            import httpx

            raise httpx.ConnectError("connection refused")
        return _FakeVLLMResponse(self._payload, self._status_code)

    async def __aenter__(self) -> "_FakeVLLMClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_vllm_httpx(monkeypatch: pytest.MonkeyPatch, client: _FakeVLLMClient) -> None:
    import phoson_agent.plugins.context_window as mod

    def _factory(*args: object, **kwargs: object) -> _FakeVLLMClient:
        return client

    monkeypatch.setattr(mod.httpx, "AsyncClient", _factory)


def _vllm_payload(model_id: str, max_model_len: int) -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "owned_by": "vllm",
                "max_model_len": max_model_len,
            }
        ],
    }


@pytest.mark.asyncio
class TestResolverVLLM:
    async def test_resolves_max_model_len(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeVLLMClient(_vllm_payload("Qwen3.8-27B-FP8", 262_144))
        _patch_vllm_httpx(monkeypatch, client)
        r = ContextWindowResolver(vllm_base_url="http://localhost:8383/v1")

        assert await r.resolve("vllm", "Qwen3.8-27B-FP8") == 262_144
        # Queried the /models endpoint under the configured base URL.
        assert client.get_calls == ["http://localhost:8383/v1/models"]

    async def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeVLLMClient(_vllm_payload("m1", 4096))
        _patch_vllm_httpx(monkeypatch, client)
        r = ContextWindowResolver()

        assert await r.resolve("vllm", "m1") == 4096
        assert await r.resolve("vllm", "m1") == 4096
        # Second resolve served from cache — no extra HTTP call.
        assert len(client.get_calls) == 1

    async def test_unknown_model_id_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeVLLMClient(_vllm_payload("m1", 4096))
        _patch_vllm_httpx(monkeypatch, client)
        r = ContextWindowResolver()

        with pytest.warns(UserWarning):
            assert await r.resolve("vllm", "other-model") == DEFAULT_CONTEXT_WINDOW

    async def test_unreachable_server_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        import logging

        client = _FakeVLLMClient(None)  # raises on get()
        _patch_vllm_httpx(monkeypatch, client)
        r = ContextWindowResolver(vllm_base_url="http://127.0.0.1:9/v1")

        with caplog.at_level(
            logging.WARNING, logger="phoson_agent.plugins.context_window"
        ):
            assert await r.resolve("vllm", "m1") == DEFAULT_CONTEXT_WINDOW
        assert any(
            "vLLM context window lookup failed" in record.message
            for record in caplog.records
        )

    async def test_default_base_url_used_when_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeVLLMClient(_vllm_payload("m1", 8192))
        _patch_vllm_httpx(monkeypatch, client)
        r = ContextWindowResolver()

        assert await r.resolve("vllm", "m1") == 8192
        assert client.get_calls == ["http://localhost:8000/v1/models"]


# ── Resolver: cache ─────────────────────────────────────────────────


def test_clear_cache():
    r = ContextWindowResolver()
    r._ollama_cache["llama3"] = 8192
    r._openrouter_cache["anthropic/claude"] = 200_000
    r._vllm_cache["Qwen3.8-27B-FP8"] = 262_144
    r.clear_cache()
    assert r._ollama_cache == {}
    assert r._openrouter_cache == {}
    assert r._vllm_cache == {}


# ── Resolver: logging policy (issue #23) ─────────────────────────────


@pytest.mark.asyncio
async def test_ollama_fallback_logs_warning_for_traceability(caplog):
    """A failed Ollama context lookup must leave a WARNING log record
    (in addition to the user-facing UserWarning) so silent fallbacks
    remain diagnosable."""
    import logging
    import warnings

    resolver = ContextWindowResolver(ollama_base_url="http://127.0.0.1:9")
    # The resolver also emits a user-facing UserWarning; swallow it here
    # because this test only cares about the log record.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with caplog.at_level(
            logging.WARNING, logger="phoson_agent.plugins.context_window"
        ):
            result = await resolver.resolve("ollama", "nonexistent-model")

    assert result == DEFAULT_CONTEXT_WINDOW
    assert any(
        "Ollama context window lookup failed" in record.message
        for record in caplog.records
    )
