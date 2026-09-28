"""Tests for the inline (numbered-list) pickers used by the classic REPL.

The classic REPL is line-oriented; ``/model``, ``/provider``, ``/theme`` and
``/sessions pick`` print a numbered list and read a selection with the REPL's
own prompt. These tests cover the numbering/resolution and each picker's
result mapping (the interactive prompt itself is mocked).
"""

import datetime

import pytest

from phoson_cli.theme import load_theme
from phoson_cli.models import ModelOption
from phoson_cli.inline_picker import InlineOption, _resolve, pick_inline
from phoson_agent.sessions.models import SessionMeta

# ── Resolution ───────────────────────────────────────────────────────────────


def test_resolve_by_number() -> None:
    options = [InlineOption("a", "Alpha"), InlineOption("b", "Beta")]
    assert _resolve("1", options) == "a"
    assert _resolve("2", options) == "b"
    assert _resolve("0", options) is None
    assert _resolve("9", options) is None


def test_resolve_by_value_and_display() -> None:
    options = [InlineOption("a", "Alpha"), InlineOption("b", "Beta")]
    assert _resolve("a", options) == "a"
    assert _resolve("Alpha", options) == "a"


def test_resolve_fuzzy_and_empty() -> None:
    options = [
        InlineOption("openai/gpt-4o", "GPT-4o"),
        InlineOption("anthropic/claude", "Claude"),
    ]
    assert _resolve("gpt", options) == "openai/gpt-4o"
    assert _resolve("", options) is None
    assert _resolve("zzz", options) is None


# ── pick_inline ──────────────────────────────────────────────────────────────


def _fake_read(line: str):
    async def _read(message, theme):
        return line

    return _read


@pytest.mark.asyncio
async def test_pick_inline_prints_list_and_returns_choice(monkeypatch, capsys) -> None:
    monkeypatch.setattr("phoson_cli.inline_picker._read_line", _fake_read("2"))
    options = [InlineOption("a", "Alpha"), InlineOption("b", "Beta")]

    assert await pick_inline("model", options) == "b"

    out = capsys.readouterr().out
    assert "Select model:" in out
    assert "1. Alpha" in out
    assert "2. Beta" in out


@pytest.mark.asyncio
async def test_pick_inline_marks_the_current_value(monkeypatch, capsys) -> None:
    monkeypatch.setattr("phoson_cli.inline_picker._read_line", _fake_read(""))
    options = [InlineOption("a", "Alpha"), InlineOption("b", "Beta")]

    await pick_inline("model", options, current="b")

    out = capsys.readouterr().out
    assert "▶  2. Beta" in out


@pytest.mark.asyncio
async def test_pick_inline_empty_options_returns_none() -> None:
    assert await pick_inline("model", []) is None


@pytest.mark.asyncio
async def test_pick_inline_cancel_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("phoson_cli.inline_picker._read_line", _fake_read(""))
    assert await pick_inline("model", [InlineOption("a", "Alpha")]) is None


@pytest.mark.asyncio
async def test_pick_inline_prints_notice(monkeypatch, capsys) -> None:
    monkeypatch.setattr("phoson_cli.inline_picker._read_line", _fake_read(""))
    await pick_inline("model", [InlineOption("a", "Alpha")], notice="⚠ provider down")
    assert "⚠ provider down" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_read_line_reuses_the_registered_session(monkeypatch) -> None:
    from phoson_cli import inline_picker

    state: dict = {}

    class FakeSession:
        def __init__(self) -> None:
            self.completer = "C"
            self.complete_while_typing = True
            self.key_bindings = "K"
            self.bottom_toolbar = "T"
            self.style = "S"
            self.reserve_space_for_menu = 6

        async def prompt_async(self, message):
            state["used"] = self
            return "1"

    fake = FakeSession()
    inline_picker.set_prompt_session(fake)
    try:
        line = await inline_picker._read_line("model # ", load_theme())
    finally:
        inline_picker.set_prompt_session(None)

    assert line == "1"
    assert state["used"] is fake  # reused the REPL session, not a new one
    # The prompt config is restored so the REPL prompt is unaffected.
    assert fake.completer == "C"
    assert fake.complete_while_typing is True
    assert fake.key_bindings == "K"
    assert fake.bottom_toolbar == "T"
    assert fake.style == "S"
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
