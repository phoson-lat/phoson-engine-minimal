"""Tests for the inline (completion-menu) pickers used by the classic REPL.

The classic REPL is line-oriented; ``/model``, ``/provider``, ``/theme`` and
``/sessions pick`` present a prompt_toolkit completion menu instead of a
full-screen ``Application``. These tests cover the completer's filtering and
each picker's result mapping (the interactive prompt itself is mocked).
"""

import datetime

import pytest
from prompt_toolkit.document import Document

from phoson_cli.models import ModelOption
from phoson_cli.inline_picker import InlineOption, pick_inline, _OptionCompleter
from phoson_agent.sessions.models import SessionMeta

# ── Completer ────────────────────────────────────────────────────────────────


def _completion_texts(options, query, *, current=None):
    completer = _OptionCompleter(options, current=current)
    document = Document(text=query, cursor_position=len(query))
    return [c.text for c in completer.get_completions(document, None)]


def test_completer_returns_all_options_for_empty_query() -> None:
    options = [InlineOption("a", "Alpha"), InlineOption("b", "Beta")]
    assert _completion_texts(options, "") == ["a", "b"]


def test_completer_filters_fuzzy_subsequence() -> None:
    options = [
        InlineOption("anthropic/claude", "Claude"),
        InlineOption("openai/gpt-4o", "GPT-4o"),
    ]
    assert _completion_texts(options, "gpt") == ["openai/gpt-4o"]
    assert _completion_texts(options, "g4o") == ["openai/gpt-4o"]


def test_completer_no_match_yields_nothing() -> None:
    options = [InlineOption("a", "Alpha")]
    assert _completion_texts(options, "zzz") == []


def test_completer_marks_the_current_value() -> None:
    options = [InlineOption("a", "Alpha"), InlineOption("b", "Beta")]
    completer = _OptionCompleter(options, current="b")
    document = Document(text="", cursor_position=0)
    displays = [c.display_text for c in completer.get_completions(document, None)]
    assert displays[0] == "  Alpha"
    assert displays[1] == "▶ Beta"


# ── pick_inline ──────────────────────────────────────────────────────────────


class _FakeBuffer:
    def __init__(self, state: dict) -> None:
        self._state = state

    def start_completion(self, select_first: bool = False) -> None:
        self._state["started"] = select_first


class _FakeSession:
    """Minimal stand-in for PromptSession (attributes + prompt_async)."""

    def __init__(self, result: str, state: dict, **kwargs) -> None:
        self._result = result
        self._state = state
        self.completer = kwargs.get("completer")
        self.key_bindings = kwargs.get("key_bindings")
        self.bottom_toolbar = kwargs.get("bottom_toolbar")
        self.style = kwargs.get("style")
        self.reserve_space_for_menu = kwargs.get("reserve_space_for_menu")
        self.default_buffer = _FakeBuffer(state)

    async def prompt_async(self, message, pre_run=None):
        self._state["used"] = self
        if pre_run is not None:
            pre_run()
        return self._result


def _patch_session(monkeypatch, result: str) -> dict:
    """Replace PromptSession with a fake that returns *result* immediately."""
    state: dict = {}

    def factory(**kwargs):
        return _FakeSession(result, state, **kwargs)

    monkeypatch.setattr("phoson_cli.inline_picker.PromptSession", factory)
    return state


@pytest.mark.asyncio
async def test_pick_inline_empty_options_returns_none() -> None:
    assert await pick_inline("model", []) is None


@pytest.mark.asyncio
async def test_pick_inline_returns_selected_value_and_shows_menu(monkeypatch) -> None:
    state = _patch_session(monkeypatch, "openai/gpt-4o")
    options = [InlineOption("openai/gpt-4o", "GPT-4o"), InlineOption("x", "X")]

    assert await pick_inline("model", options) == "openai/gpt-4o"
    assert state["started"] is True  # menu shown before the first keystroke


@pytest.mark.asyncio
async def test_pick_inline_cancel_returns_none(monkeypatch) -> None:
    _patch_session(monkeypatch, "")
    assert await pick_inline("model", [InlineOption("a", "Alpha")]) is None


@pytest.mark.asyncio
async def test_pick_inline_unmatched_text_returns_none(monkeypatch) -> None:
    _patch_session(monkeypatch, "not-an-option")
    assert await pick_inline("model", [InlineOption("a", "Alpha")]) is None


@pytest.mark.asyncio
async def test_pick_inline_resolves_by_display(monkeypatch) -> None:
    _patch_session(monkeypatch, "Alpha")
    assert await pick_inline("model", [InlineOption("a", "Alpha")]) == "a"


@pytest.mark.asyncio
async def test_pick_inline_resolves_partial_text_to_best_match(monkeypatch) -> None:
    """A partially-typed value resolves to its best fuzzy match."""
    _patch_session(monkeypatch, "gpt")
    options = [
        InlineOption("openai/gpt-4o", "GPT-4o"),
        InlineOption("anthropic/claude", "Claude"),
    ]
    assert await pick_inline("model", options) == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_pick_inline_reuses_the_registered_session(monkeypatch) -> None:
    """The picker reuses the REPL's session and restores its prompt config."""
    from phoson_cli import inline_picker

    state = _patch_session(monkeypatch, "a")  # fallback factory (unused here)
    fake = _FakeSession("a", state)
    fake.completer = "ORIGINAL_COMPLETER"
    fake.key_bindings = "ORIGINAL_KB"
    fake.bottom_toolbar = "ORIGINAL_TOOLBAR"
    fake.style = "ORIGINAL_STYLE"
    fake.reserve_space_for_menu = 6
    inline_picker.set_prompt_session(fake)
    try:
        result = await pick_inline("model", [InlineOption("a", "Alpha")])
    finally:
        inline_picker.set_prompt_session(None)

    assert result == "a"
    assert state["used"] is fake  # reused the REPL session, not a new one
    # The prompt config is restored so the REPL prompt is unaffected.
    assert fake.completer == "ORIGINAL_COMPLETER"
    assert fake.key_bindings == "ORIGINAL_KB"
    assert fake.bottom_toolbar == "ORIGINAL_TOOLBAR"
    assert fake.style == "ORIGINAL_STYLE"
    assert fake.reserve_space_for_menu == 6


# ── Picker result mapping ────────────────────────────────────────────────────


def _fake_pick_inline(value):
    async def _pick(title, options, **kwargs):
        return value

    return _pick


@pytest.mark.asyncio
async def test_pick_model_maps_selection_to_provider(monkeypatch) -> None:
    from phoson_cli import model_picker

    monkeypatch.setattr(
        "phoson_cli.inline_picker.pick_inline", _fake_pick_inline("openai/gpt-4o")
    )
    models = [
        ModelOption(id="openai/gpt-4o", label="GPT-4o", provider="openai"),
        ModelOption(id="anthropic/claude", label="Claude", provider="anthropic"),
    ]

    result = await model_picker.pick_model(
        models, "openai/gpt-4o", current_provider="openai"
    )

    assert result.model_id == "openai/gpt-4o"
    assert result.provider == "openai"
    assert result.cancelled is False


@pytest.mark.asyncio
async def test_pick_model_cancel_returns_cancelled(monkeypatch) -> None:
    from phoson_cli import model_picker

    monkeypatch.setattr("phoson_cli.inline_picker.pick_inline", _fake_pick_inline(None))
    models = [ModelOption(id="m", label="m", provider="p")]

    result = await model_picker.pick_model(models, "m")

    assert result.cancelled is True
    assert result.model_id is None


@pytest.mark.asyncio
async def test_pick_provider_returns_selected(monkeypatch) -> None:
    from phoson_cli import provider_picker

    monkeypatch.setattr(
        "phoson_cli.inline_picker.pick_inline", _fake_pick_inline("anthropic")
    )

    result = await provider_picker.pick_provider(["openai", "anthropic"], "openai")

    assert result.provider == "anthropic"
    assert result.cancelled is False


@pytest.mark.asyncio
async def test_pick_theme_returns_selected(monkeypatch) -> None:
    from phoson_cli import theme_picker

    monkeypatch.setattr(
        "phoson_cli.inline_picker.pick_inline", _fake_pick_inline("light")
    )

    result = await theme_picker.pick_theme("dark")

    assert result.theme_name == "light"


@pytest.mark.asyncio
async def test_pick_session_returns_selected_id(monkeypatch) -> None:
    from phoson_cli import session_picker

    monkeypatch.setattr(
        "phoson_cli.inline_picker.pick_inline", _fake_pick_inline("abc123")
    )
    now = datetime.datetime.now(datetime.UTC)
    sessions = [
        SessionMeta(
            id="abc123",
            created_at=now,
            updated_at=now,
            message_count=2,
            total_cost=0.0,
            total_tokens=0,
            step_count=1,
            last_model="m",
        )
    ]

    result = await session_picker.pick_session(sessions, "current")

    assert result.session_id == "abc123"
    assert result.cancelled is False


@pytest.mark.asyncio
async def test_pick_session_empty_returns_cancelled() -> None:
    from phoson_cli import session_picker

    result = await session_picker.pick_session([], "current")

    assert result.cancelled is True
