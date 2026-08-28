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


# ── I-113: unified multi-provider view ──────────────────────────────────────


def test_render_models_multi_provider_shows_provider_and_unavailable() -> None:
    from phoson_cli.models import ModelOption

    models = [
        ModelOption(id="gpt-4o", label="gpt-4o", provider="openai"),
        ModelOption(
            id="claude-sonnet-4-6", label="claude-sonnet-4-6", provider="anthropic"
        ),
    ]
    rendered = _render_models(
        models=models,
        current_model="gpt-4o",
        current_provider="openai",
        selected=0,
        page=0,
        page_size=10,
        unavailable=[("groq", "ConnectError: boom")],
    )
    text = "".join(part for _, part in rendered)

    # Multi-provider rows carry the provider in parentheses.
    assert "(openai)" in text
    assert "(anthropic)" in text
    # Current *pair* row is selected; the row style encodes the pair.
    rows = [t for s, t in rendered if s == "class:row.selected"]
    assert len(rows) == 1 and "(openai)" in rows[0]
    # Failed provider shows an unavailable section.
    assert "groq — unavailable: ConnectError: boom" in text


def test_render_models_current_marker_uses_pair_in_multi_mode() -> None:
    from phoson_cli.models import ModelOption

    # Same model id served by two providers: only the (id, provider) pair
    # matching the current pair gets the active marker.
    models = [
        ModelOption(id="gpt-4o", label="gpt-4o", provider="openrouter"),
        ModelOption(id="gpt-4o", label="gpt-4o", provider="openai"),
    ]
    # Select row 1 (openrouter): the current pair (openai) must still get
    # the active marker, and the openrouter row must NOT.
    rendered = _render_models(
        models=models,
        current_model="gpt-4o",
        current_provider="openai",
        selected=0,
        page=0,
        page_size=10,
    )
    active_rows = [t for s, t in rendered if s == "class:row.active"]
    assert len(active_rows) == 1
    assert "(openai)" in active_rows[0]
    selected_rows = [t for s, t in rendered if s == "class:row.selected"]
    assert len(selected_rows) == 1
    assert "(openrouter)" in selected_rows[0]


def test_unified_picker_confirm_returns_provider() -> None:
    from phoson_cli.models import ModelOption
    from phoson_cli.model_picker import (
        ModelPickerResult,
        build_unified_model_picker,
    )

    models = [
        ModelOption(id="gpt-4o", label="gpt-4o", provider="openai"),
        ModelOption(
            id="claude-sonnet-4-6", label="claude-sonnet-4-6", provider="anthropic"
        ),
    ]

    def _trigger(picker, key: str) -> None:
        from phoson_cli.pickers import BasePicker

        assert isinstance(picker, BasePicker)
        aliases = {"enter": "c-m"}
        target = aliases.get(key, key)
        for binding in picker._kb.bindings:
            for k in binding.keys:
                if str(getattr(k, "value", str(k))).lower() == target:
                    binding.handler(None)
                    return
        raise KeyError(key)

    # 1) Confirm on the current model (openai) → resolves the openai pair.
    done: list[ModelPickerResult] = []
    picker = build_unified_model_picker(
        models,
        current_model="gpt-4o",
        current_provider="openai",
        on_done=done.append,
    )
    _trigger(picker, "enter")
    assert done and done[0] == ModelPickerResult(model_id="gpt-4o", provider="openai")

    # 2) Move to the second row (anthropic) and confirm → cross-provider
    #    selection carries the anthropic provider.
    done.clear()
    picker2 = build_unified_model_picker(
        models,
        current_model="gpt-4o",
        current_provider="openai",
        on_done=done.append,
    )
    _trigger(picker2, "down")
    _trigger(picker2, "enter")
    assert done and done[0] == ModelPickerResult(
        model_id="claude-sonnet-4-6", provider="anthropic"
    )
