from phoson_cli.model_picker import (
    _filter_models,
    _render_models,
    _format_context_length,
)
from phoson_cli.model_selector import ModelOption


def test_render_models_shows_current_marker() -> None:
    models = [
        ModelOption(id="gpt-4o-mini", label="gpt-4o-mini", provider="openai"),
        ModelOption(id="gpt-4.1", label="gpt-4.1", provider="openai"),
    ]

    rendered = _render_models(
        models=models,
        current_model="gpt-4.1",
        selected=0,
        page=0,
        page_size=10,
    )
    text = "".join(part for _, part in rendered)

    assert "Available Models" in text
    assert "Search:" in text
    assert "gpt-4.1" in text
    assert "gpt-4o-mini" in text


def test_format_context_length_is_human_readable() -> None:
    assert _format_context_length(128000) == "128k"
    assert _format_context_length(1_000_000) == "1M"
    assert _format_context_length(None) == "—"


def test_filter_models_matches_fuzzy_query() -> None:
    models = [
        ModelOption(
            id="openai/gpt-4.1-mini", label="openai/gpt-4.1-mini", provider="openrouter"
        ),
        ModelOption(
            id="anthropic/claude-3.5-haiku",
            label="anthropic/claude-3.5-haiku",
            provider="openrouter",
        ),
        ModelOption(
            id="google/gemini-2.5-flash",
            label="google/gemini-2.5-flash",
            provider="openrouter",
        ),
    ]

    filtered = _filter_models(models, "g41m")

    assert filtered
    assert filtered[0].id == "openai/gpt-4.1-mini"


def test_render_models_empty_filter_state() -> None:
    rendered = _render_models(
        models=[],
        current_model="gpt-4.1",
        selected=0,
        page=0,
        page_size=10,
        query="zzz",
    )
    text = "".join(part for _, part in rendered)

    assert "No models match the current filter." in text
