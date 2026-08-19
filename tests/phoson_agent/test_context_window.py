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


# ── Resolver: cache ─────────────────────────────────────────────────


def test_clear_cache():
    r = ContextWindowResolver()
    r._ollama_cache["llama3"] = 8192
    r._openrouter_cache["anthropic/claude"] = 200_000
    r.clear_cache()
    assert r._ollama_cache == {}
    assert r._openrouter_cache == {}


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
