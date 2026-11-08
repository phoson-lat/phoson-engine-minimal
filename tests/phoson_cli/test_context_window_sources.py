"""Context-window sources: user override → provider listing → registry."""

from types import SimpleNamespace

import httpx
import pytest

# Imported up front: the Cheaper Inference lister imports it lazily, and the
# OpenAI SDK subclasses httpx.AsyncClient at import time — it must not see the
# patched client below.
import phoson_llm.chats.cheaperinference  # noqa: F401
from phoson_agent.plugins.context_window import (
    DEFAULT_CONTEXT_WINDOW,
    ContextWindowResolver,
    normalize_model_id,
)


def _resolver(*, override=None, catalog=None) -> ContextWindowResolver:
    return ContextWindowResolver(user_override=override, catalog=catalog)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Claude-Opus-5-5", "claude-opus-5-5"),
        ("anthropic/claude-opus-4-6-20250514", "claude-opus-4-6"),
        ("models/gemini-3.8-flash", "gemini-3.8-flash"),
        ("command-a-03-2025", "command-a-03-2025"),
        ("gpt-4o-2024-11-20", "gpt-4o"),
    ],
)
def test_normalize_model_id(raw: str, expected: str) -> None:
    assert normalize_model_id(raw) == expected


@pytest.mark.asyncio
async def test_resolution_order() -> None:
    calls: list[str] = []

    async def catalog(provider: str):
        calls.append(provider)
        return {"Claude-Opus-5-5": 900_000, "other": 64_000}

    def override(provider: str, model: str):
        return 300_000 if model == "pinned" else None

    r = _resolver(override=override, catalog=catalog)
    assert await r.resolve("anthropic", "pinned") == 300_000  # user setting first
    assert await r.resolve("anthropic", "claude-opus-5-5") == 900_000  # listing
    assert await r.resolve("anthropic", "claude-haiku-5-5") == 1_000_000  # registry
    assert await r.resolve("anthropic", "claude-unknown") == 200_000  # family
    assert calls == ["anthropic"]  # the listing is fetched once per provider

    r.override("anthropic", "claude-opus-5-5", 123_000)  # learned from an error
    assert await r.resolve("anthropic", "claude-opus-5-5") == 123_000


@pytest.mark.asyncio
async def test_failed_listing_falls_back_and_is_not_refetched_immediately() -> None:
    calls: list[str] = []

    async def catalog(provider: str):
        calls.append(provider)
        raise httpx.ConnectError("offline")

    r = _resolver(catalog=catalog)
    assert await r.resolve("xai", "grok-4.7") == 500_000
    assert await r.resolve("xai", "grok-4.6") == 500_000
    assert calls == ["xai"]
    assert await r.resolve("mystery", "model") == DEFAULT_CONTEXT_WINDOW


@pytest.mark.asyncio
async def test_listing_without_windows_is_cached() -> None:
    calls: list[str] = []

    async def catalog(provider: str):
        calls.append(provider)
        return {}

    r = _resolver(catalog=catalog)
    assert await r.resolve("openai", "gpt-5.5") == 1_050_000
    assert await r.resolve("openai", "gpt-4o") == 128_000
    assert calls == ["openai"]


@pytest.mark.asyncio
async def test_broken_override_hook_is_ignored() -> None:
    def override(provider: str, model: str):
        raise RuntimeError("bad models.json")

    r = _resolver(override=override)
    assert await r.resolve("openai", "gpt-5.5") == 1_050_000


def test_summarizer_uses_the_shared_resolver() -> None:
    from phoson_agent.plugins.summarizer import SummarizationMiddleware

    shared = ContextWindowResolver()
    assert SummarizationMiddleware(context_resolver=shared)._resolver is shared


def test_cli_controller_and_summarizer_share_one_resolver() -> None:
    from phoson_cli.config import PhosonConfig
    from phoson_cli.session_utils import build_summarizer, build_context_resolver

    config = PhosonConfig(provider="anthropic", model="claude-opus-5-5")
    resolver = build_context_resolver(config)
    assert build_summarizer(config, resolver)._resolver is resolver


# ─── Provider listings carry the window ──────────────────────────────────────

LISTINGS = {
    "anthropic": (
        "anthropic_api_key",
        {"data": [{"id": "claude-opus-5-5", "max_input_tokens": 1_000_000}]},
        {"claude-opus-5-5": 1_000_000},
    ),
    "gemini": (
        "gemini_api_key",
        {"models": [{"name": "models/gemini-3.8-flash", "inputTokenLimit": 1048576}]},
        {"gemini-3.8-flash": 1_048_576},
    ),
    "groq": (
        "groq_api_key",
        {"data": [{"id": "llama-3.1-8b-instant", "context_window": 131072}]},
        {"llama-3.1-8b-instant": 131_072},
    ),
    "mistral": (
        "mistral_api_key",
        {"data": [{"id": "mistral-large-latest", "max_context_length": 262144}]},
        {"mistral-large-latest": 262_144},
    ),
    "fireworks": (
        "fireworks_api_key",
        {"models": [{"name": "accounts/f/models/x", "contextLength": 163840}]},
        {"accounts/f/models/x": 163_840},
    ),
    "cohere": (
        "cohere_api_key",
        {"models": [{"name": "command-a-03-2025", "context_length": 256000.0}]},
        {"command-a-03-2025": 256_000},
    ),
    "xai": (
        "xai_api_key",
        {"data": [{"id": "grok-4.7", "context_length": 500000}]},
        {"grok-4.7": 500_000},
    ),
    "deepseek": (
        "deepseek_api_key",
        {"data": [{"id": "deepseek-flash", "context_window": 1048576}, {"id": "x"}]},
        {"deepseek-flash": 1_048_576},
    ),
    "cheaperinference": (
        "cheaperinference_api_key",
        {
            "data": [
                {"id": "claude-sonnet-5.5", "type": "chat", "context_length": 1000000}
            ]
        },
        {"claude-sonnet-5.5": 1_000_000},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(LISTINGS))
async def test_provider_context_windows(monkeypatch, provider: str) -> None:
    from phoson_cli import model_selector

    key_field, payload, expected = LISTINGS[provider]
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        model_selector.httpx,
        "AsyncClient",
        lambda **kw: real(transport=transport, **kw),
    )
    config = SimpleNamespace(provider="openai", model="m", **{key_field: "k"})
    windows = await model_selector.provider_context_windows(config, provider)  # type: ignore[arg-type]
    assert windows == expected


@pytest.mark.asyncio
async def test_provider_context_windows_reports_listing_failure(monkeypatch) -> None:
    from phoson_cli import model_selector

    transport = httpx.MockTransport(lambda _r: httpx.Response(500))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        model_selector.httpx,
        "AsyncClient",
        lambda **kw: real(transport=transport, **kw),
    )
    config = SimpleNamespace(provider="xai", model="m", xai_api_key="k")
    assert await model_selector.provider_context_windows(config, "xai") is None  # type: ignore[arg-type]
