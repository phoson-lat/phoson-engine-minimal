"""Unit tests for ModelCache and ModelArgCompleter (inline /model autocomplete).

Model selection follows the reference prototype's approach: a plain
fuzzy dropdown fed by a background-refreshed id list, rather than a
modal picker.
"""

from unittest.mock import patch

import pytest
from prompt_toolkit.document import Document

from phoson_cli.config import PhosonConfig
from phoson_cli.model_selector import ModelOption, ProviderListing
from phoson_cli.fullscreen.completer import ModelArgCompleter
from phoson_cli.fullscreen.model_cache import ModelCache


@pytest.mark.asyncio
async def test_refresh_populates_model_ids() -> None:
    cache = ModelCache()
    listings = [
        ProviderListing(
            provider="openai",
            options=[
                ModelOption(id="openai/gpt-4o", label="GPT-4o", provider="openai"),
            ],
        ),
        ProviderListing(
            provider="anthropic",
            options=[
                ModelOption(
                    id="anthropic/claude-opus-5", label="Opus 5", provider="anthropic"
                ),
            ],
        ),
    ]
    with patch(
        "phoson_cli.fullscreen.model_cache.list_models_for_providers",
        return_value=listings,
    ):
        await cache.refresh(PhosonConfig(provider="openai"))

    assert cache.model_ids == ["openai/gpt-4o", "anthropic/claude-opus-5"]
    # I-113: the cache also tracks which provider owns each id, so the
    # inline dropdown can show it.
    assert cache.model_providers == {
        "openai/gpt-4o": "openai",
        "anthropic/claude-opus-5": "anthropic",
    }


@pytest.mark.asyncio
async def test_refresh_keeps_previous_list_on_failure() -> None:
    cache = ModelCache()
    cache.model_ids = ["openai/gpt-4o"]

    async def _boom(config, providers):
        raise RuntimeError("network down")

    with patch("phoson_cli.fullscreen.model_cache.list_models_for_providers", _boom):
        await cache.refresh(PhosonConfig(provider="openai"))

    assert cache.model_ids == ["openai/gpt-4o"]  # unchanged, not cleared


def _meta_to_text(meta: object) -> str:
    """prompt_toolkit normalises display_meta into a FormattedText
    (list of (style, text) tuples); flatten it back to a plain string."""
    if isinstance(meta, (list, tuple)):
        return "".join(text for _style, text in meta)
    return str(meta or "")


def _complete(completer: ModelArgCompleter, text: str) -> list[tuple[str, str]]:
    doc = Document(text, len(text))
    return [
        (c.text, _meta_to_text(c.display_meta))
        for c in completer.get_completions(doc, None)
    ]


def test_completes_model_arg_after_slash_model_prefix() -> None:
    cache = ModelCache()
    cache.model_ids = [
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "openai/gpt-4o",
    ]
    completer = ModelArgCompleter(cache)

    results = _complete(completer, "/model claude")

    assert "anthropic/claude-opus-5" in [t for t, _ in results]
    assert "anthropic/claude-sonnet-5" in [t for t, _ in results]
    assert "openai/gpt-4o" not in [t for t, _ in results]


def test_model_dropdown_shows_provider_as_display_meta() -> None:
    """I-113: each /model suggestion carries its provider (dimmed)."""
    cache = ModelCache()
    cache.model_ids = [
        "anthropic/claude-opus-5",
        "openai/gpt-4o",
        "minimax/minimax-m2.7",
    ]
    cache.model_providers = {
        "anthropic/claude-opus-5": "anthropic",
        "openai/gpt-4o": "openai",
        "minimax/minimax-m2.7": "openrouter",
    }
    completer = ModelArgCompleter(cache)

    results = dict(_complete(completer, "/model "))

    # The inserted text stays the bare id; the provider is display_meta.
    assert results["anthropic/claude-opus-5"] == "anthropic"
    assert results["openai/gpt-4o"] == "openai"
    assert results["minimax/minimax-m2.7"] == "openrouter"


def test_model_dropdown_without_provider_map_keeps_plain_meta() -> None:
    """Cache populated before the provider map existed → no meta, no crash."""
    cache = ModelCache()
    cache.model_ids = ["openai/gpt-4o"]
    completer = ModelArgCompleter(cache)

    assert _complete(completer, "/model gpt") == [("openai/gpt-4o", "")]


def test_subagent_dropdown_does_not_show_provider() -> None:
    """The sub-agent model always runs on the active provider — no tag."""
    cache = ModelCache()
    cache.model_ids = ["anthropic/claude-haiku-5"]
    cache.model_providers = {"anthropic/claude-haiku-5": "anthropic"}
    completer = ModelArgCompleter(cache)

    assert _complete(completer, "/subagent-model haiku") == [
        ("anthropic/claude-haiku-5", "")
    ]


def test_no_completions_outside_model_arg_context() -> None:
    cache = ModelCache()
    cache.model_ids = ["openai/gpt-4o"]
    completer = ModelArgCompleter(cache)

    assert _complete(completer, "hello") == []
    assert _complete(completer, "/model") == []  # no trailing space yet
    assert _complete(completer, "/provider open") == []


def test_reads_the_cache_live_not_a_frozen_snapshot() -> None:
    """The completer must see updates to model_ids after construction —

    it's built once at app startup, long before the background refresh
    (fired from run_async) has populated anything.
    """
    cache = ModelCache()
    completer = ModelArgCompleter(cache)

    assert _complete(completer, "/model gpt") == []

    cache.model_ids = ["openai/gpt-4o"]
    assert _complete(completer, "/model gpt") == [("openai/gpt-4o", "")]
