"""Unit tests for ModelCache and ModelArgCompleter (inline /model autocomplete).

Model selection follows the reference prototype's approach: a plain
fuzzy dropdown fed by a background-refreshed id list, rather than a
modal picker.
"""

from unittest.mock import patch

import pytest
from prompt_toolkit.document import Document

from phoson_cli.config import PhosonConfig
from phoson_cli.model_selector import ModelOption
from phoson_cli.fullscreen.completer import ModelArgCompleter
from phoson_cli.fullscreen.model_cache import ModelCache


@pytest.mark.asyncio
async def test_refresh_populates_model_ids() -> None:
    cache = ModelCache()
    models = [
        ModelOption(id="openai/gpt-4o", label="GPT-4o", provider="openai"),
        ModelOption(id="anthropic/claude-opus-5", label="Opus 5", provider="anthropic"),
    ]
    with patch(
        "phoson_cli.fullscreen.model_cache.list_available_models",
        return_value=models,
    ):
        await cache.refresh(PhosonConfig(provider="openai"))

    assert cache.model_ids == ["openai/gpt-4o", "anthropic/claude-opus-5"]


@pytest.mark.asyncio
async def test_refresh_keeps_previous_list_on_failure() -> None:
    cache = ModelCache()
    cache.model_ids = ["openai/gpt-4o"]

    async def _boom(config):
        raise RuntimeError("network down")

    with patch("phoson_cli.fullscreen.model_cache.list_available_models", _boom):
        await cache.refresh(PhosonConfig(provider="openai"))

    assert cache.model_ids == ["openai/gpt-4o"]  # unchanged, not cleared


def _complete(completer: ModelArgCompleter, text: str) -> list[str]:
    doc = Document(text, len(text))
    return [c.text for c in completer.get_completions(doc, None)]


def test_completes_model_arg_after_slash_model_prefix() -> None:
    cache = ModelCache()
    cache.model_ids = [
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "openai/gpt-4o",
    ]
    completer = ModelArgCompleter(cache)

    results = _complete(completer, "/model claude")

    assert "anthropic/claude-opus-5" in results
    assert "anthropic/claude-sonnet-5" in results
    assert "openai/gpt-4o" not in results


def test_completes_model_arg_after_subagent_model_prefix() -> None:
    cache = ModelCache()
    cache.model_ids = ["anthropic/claude-haiku-5"]
    completer = ModelArgCompleter(cache)

    assert _complete(completer, "/subagent-model haiku") == ["anthropic/claude-haiku-5"]


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
    assert _complete(completer, "/model gpt") == ["openai/gpt-4o"]
