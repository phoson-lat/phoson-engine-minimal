"""Tests for E4 — interactive themes and light/dark auto-detection.

Covers the four layers:

* ``phoson_cli.terminal_theme`` — COLORFGBG parsing, OSC 11 response
  parsing and the injectable terminal query (no real TTY in tests).
* ``phoson_cli.theme`` — ``suggest_theme`` and ``get_theme``.
* ``phoson_cli.theme_picker`` — the live-preview picker (bindings driven
  programmatically, same pattern as ``test_picker_base``).
* Wiring — the ``/theme`` command, ``PhosonRepl.apply_theme`` /
  ``PhosonApp.apply_theme``, both command hosts, the first-run
  suggestion in ``__main__`` and the setup wizard's theme question.
"""

import asyncio
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.theme import (
    ANSI,
    DARK,
    LIGHT,
    NO_COLOR,
    VALID_NAMES,
    get_theme,
    suggest_theme,
)
from phoson_cli.config import PhosonConfig, has_persisted_theme
from phoson_cli.terminal_theme import (
    parse_colorfgbg,
    parse_osc11_response,
    detect_terminal_theme,
    query_terminal_bg_light,
)

# ─── COLORFGBG parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("0;15", True),  # black fg, white bg — classic light terminal
        ("15;0", False),  # white fg, black bg — classic dark terminal
        ("0;0", False),  # black bg
        ("0;7", True),  # white bg
        ("light;dark", False),  # tmux-style
        ("dark;light", True),  # tmux-style
        ("light", True),  # single word
        ("dark", False),
        ("0;2", True),  # green bg (bright enough for dark foreground)
        ("0;3", True),  # yellow bg
        ("0;4", False),  # blue bg
        ("0;5", False),  # magenta bg
        ("0;6", True),  # cyan bg
        ("8;8", True),  # bright black bg ≈ grey
        ("0;9", True),  # bright red bg
        ("0;255", None),  # not a 16-color index
        ("garbage", None),
        ("0;1x", None),
    ],
)
def test_parse_colorfgbg(value, expected) -> None:
    assert parse_colorfgbg(value) is expected


# ─── OSC 11 response parsing ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\x1b]11;rgb:255;255;255\x07", True),
        (b"\x1b]11;rgb:0;0;0\x07", False),
        (b"\x1b]11;#ffffff\x07", True),
        (b"\x1b]11;#1e1e1e\x07", False),
        (b"\x1b]11;#6f2dbd\x07", False),  # the dark theme's purple
        (b"\x1b]11;#f2eef8\x07", True),  # the light theme's panel bg
        (b"\x1b]11;255;255;255\x1b\\", True),  # decimal + ST terminator
        (b"\x1b]11;15;15;15\x1b\\", False),
        (b"\x1b]11;255,255,255\x07", True),  # comma-separated decimal
        # Other escape sequences before the response (real terminals
        # interleave).
        (b"\x1b]11;?\x07\x1b]11;rgb:100;100;100\x07", False),
        (b"\x1b[0m\x1b]11;#d0d0d0\x07", True),
        # No response at all / unrelated replies.
        (b"", None),
        (b"\x1b]11;?\x07", None),  # the query echoed back, no answer
        (b"\x1b]10;rgb:0;0;0\x07", None),  # wrong OSC (foreground)
        (b"\x1b]11;rgb:12;34\x07", None),  # malformed color
        (b"\x1b]11;#abc\x07", None),  # shorthand not understood
    ],
)
def test_parse_osc11_response(raw, expected) -> None:
    assert parse_osc11_response(raw) is expected


# ─── query_terminal_bg_light (injectable IO) ─────────────────────────────────


@pytest.fixture
def _fake_tty(monkeypatch):
    """The probe's fd guard must pass for the injected fd in tests."""
    monkeypatch.setattr("phoson_cli.terminal_theme.os.isatty", lambda fd: fd == 7)


def test_query_returns_detection_from_injected_read(_fake_tty) -> None:
    sent: list[bytes] = []
    light = query_terminal_bg_light(
        tty_fd=7,
        write=lambda data: sent.append(data),
        read=lambda: b"\x1b]11;#ffffff\x07",
    )
    dark = query_terminal_bg_light(
        tty_fd=7,
        write=lambda data: None,
        read=lambda: b"\x1b]11;#000000\x07",
    )
    assert light is True
    assert dark is False
    assert sent == [b"\x1b]11;?\x07"]  # the OSC 11 query was sent


def test_query_no_response_is_none(_fake_tty) -> None:
    assert (
        query_terminal_bg_light(tty_fd=7, write=lambda d: None, read=lambda: b"")
        is None
    )


def test_query_non_tty_skips_io(monkeypatch) -> None:
    """A fd that is not a TTY — no IO attempted, result None."""
    monkeypatch.setattr("phoson_cli.terminal_theme.os.isatty", lambda fd: False)
    sent: list[bytes] = []
    assert (
        query_terminal_bg_light(
            tty_fd=7, write=lambda d: sent.append(d), read=lambda: b""
        )
        is None
    )
    assert sent == []


def test_query_no_real_tty_by_default(monkeypatch) -> None:
    """Default fds (0/1) are captured in pytest — the probe gives up."""
    assert query_terminal_bg_light() is None


def test_query_injected_read_never_touches_real_fd(monkeypatch) -> None:
    """Regression (CI): a fake fd that does NOT exist as a real fd must
    not break the injected fast path — no select(), no termios, no
    ``OSError: Bad file descriptor`` leaking through."""
    monkeypatch.setattr("phoson_cli.terminal_theme.os.isatty", lambda fd: fd == 987654)
    assert (
        query_terminal_bg_light(
            tty_fd=987654, write=lambda d: None, read=lambda: b"\x1b]11;#000000\x07"
        )
        is False
    )


def test_query_oserror_is_none(_fake_tty) -> None:
    def _boom() -> bytes:
        raise OSError("hang up")

    assert query_terminal_bg_light(tty_fd=7, write=lambda d: None, read=_boom) is None


# ─── detect_terminal_theme (layering) ────────────────────────────────────────


def test_detect_prefers_colorfgbg_over_osc(monkeypatch) -> None:
    """COLORFGBG alone must decide — the OSC 11 probe must not run."""
    monkeypatch.setenv("COLORFGBG", "0;15")

    def _must_not_query(**kw):
        raise AssertionError("OSC 11 must not be queried when COLORFGBG is set")

    monkeypatch.setattr(
        "phoson_cli.terminal_theme.query_terminal_bg_light", _must_not_query
    )
    assert detect_terminal_theme() is True


def test_detect_env_wins_without_querying(monkeypatch) -> None:
    monkeypatch.setenv("COLORFGBG", "15;0")
    monkeypatch.setattr(
        "phoson_cli.terminal_theme.query_terminal_bg_light",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not query")),
    )
    assert detect_terminal_theme() is False


def test_detect_falls_through_to_query(monkeypatch) -> None:
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setattr(
        "phoson_cli.terminal_theme.query_terminal_bg_light",
        lambda **kw: True,
    )
    assert detect_terminal_theme() is True


def test_detect_all_layers_none(monkeypatch) -> None:
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setattr(
        "phoson_cli.terminal_theme.query_terminal_bg_light",
        lambda **kw: None,
    )
    assert detect_terminal_theme() is None


# ─── suggest_theme ───────────────────────────────────────────────────────────


def test_suggest_theme_decisions() -> None:
    assert suggest_theme(detected_light=True, has_persisted=False) == "light"
    assert suggest_theme(detected_light=False, has_persisted=False) == "dark"
    assert suggest_theme(detected_light=None, has_persisted=False) is None
    assert suggest_theme(detected_light=True, has_persisted=True) is None
    assert (
        suggest_theme(
            detected_light=True, has_persisted=False, env_requests_no_color=True
        )
        is None
    )


# ─── get_theme ───────────────────────────────────────────────────────────────


def test_get_theme_direct_lookup(monkeypatch) -> None:
    assert get_theme("light") is LIGHT
    assert get_theme("  ANSI ") is ANSI
    assert get_theme("no-color") is NO_COLOR
    assert get_theme("neon") is None
    # Environment overrides (load_theme's job) are deliberately ignored.
    monkeypatch.setenv("NO_COLOR", "1")
    assert get_theme("dark") is DARK


def test_valid_names_cover_all_tiers() -> None:
    assert set(VALID_NAMES) == {"dark", "light", "ansi", "no-color"}


# ─── has_persisted_theme ─────────────────────────────────────────────────────


def test_has_persisted_theme_no_file_no_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    assert has_persisted_theme(tmp_path / "missing.toml") is False


def test_has_persisted_theme_env(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_THEME", "light")
    assert has_persisted_theme(Path("/nonexistent/config.toml")) is True


def test_has_persisted_theme_from_file(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nmodel = "x"\ntheme = "light"\n', encoding="utf-8")
    assert has_persisted_theme(cfg) is True


def test_has_persisted_theme_file_without_theme_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nmodel = "x"\n', encoding="utf-8")
    assert has_persisted_theme(cfg) is False


def test_has_persisted_theme_malformed_file(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[defaults\n", encoding="utf-8")  # broken TOML
    assert has_persisted_theme(cfg) is False


# ─── theme picker ────────────────────────────────────────────────────────────


def _trigger(picker, key: str) -> None:
    """Drive one key binding (same pattern as test_picker_base)."""
    aliases = {"enter": "c-m", "return": "c-m"}
    target = aliases.get(key.lower(), key.lower())
    for binding in picker._kb.bindings:
        for k in binding.keys:
            if str(getattr(k, "value", "")).lower() == target:
                binding.handler(None)
                return
    raise KeyError(key)


def test_theme_rows_cover_all_tiers() -> None:
    from phoson_cli.theme_picker import _THEME_ROWS

    assert {name for name, _ in _THEME_ROWS} == set(VALID_NAMES)


def test_theme_picker_selects_current_initially() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("light")
    assert picker._render()  # renders without error
    frame_text = "".join(text for _style, text in picker._render())
    # The selected row (light, index 1) carries the selected marker.
    assert "▸  2  light" in frame_text
    assert "(current)" in frame_text
    assert "(detected)" not in frame_text


def test_theme_picker_marks_detected() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("dark", detected_name="dark")
    frame_text = "".join(text for _style, text in picker._render())
    assert "(current · detected)" in frame_text


def test_theme_picker_navigation_and_enter() -> None:
    from phoson_cli.theme_picker import ThemePickerResult, build_theme_picker

    results: list = []
    picker = build_theme_picker(
        "dark",
        on_done=lambda r: results.append(r),
        invalidate=lambda: None,
    )
    _trigger(picker, "down")  # dark -> light
    _trigger(picker, "down")  # light -> ansi
    _trigger(picker, "enter")

    assert len(results) == 1
    assert isinstance(results[0], ThemePickerResult)
    assert results[0].theme_name == "ansi"
    assert results[0].cancelled is False


def test_theme_picker_escape_cancels() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    results: list = []
    picker = build_theme_picker(
        "dark", on_done=lambda r: results.append(r), invalidate=lambda: None
    )
    _trigger(picker, "escape")

    assert len(results) == 1
    assert results[0].theme_name is None
    assert results[0].cancelled is True


def test_theme_picker_wrap_at_ends() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("dark")
    _trigger(picker, "up")  # already at top: stays
    frame = "".join(t for _s, t in picker._render())
    assert "▸  1  dark" in frame
    # Walk to the bottom.
    _trigger(picker, "down")
    _trigger(picker, "down")
    _trigger(picker, "down")
    _trigger(picker, "down")  # past the end: stays
    frame = "".join(t for _s, t in picker._render())
    assert "▸  4  no-color" in frame


def test_theme_picker_preview_shows_selected_theme() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("dark")
    frame_text = "".join(t for _s, t in picker._render())
    assert "Preview — dark" in frame_text
    assert "═══" in frame_text  # the phos-ascii art
    assert "phoson" in frame_text
    # Token swatch labels.
    for token in ("text", "muted", "accent", "ok", "err", "warn", "reasoning"):
        assert token in frame_text

    _trigger(picker, "down")  # -> light
    frame_text = "".join(t for _s, t in picker._render())
    assert "Preview — light" in frame_text


def test_theme_picker_preview_escapes_are_parsed() -> None:
    """The banner preview must arrive as (style, text) fragments — raw
    SGR escapes inside fragment text would render literally."""
    from prompt_toolkit.formatted_text import to_plain_text

    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("dark")
    frame = picker._render()
    for style, text in frame:
        assert "\x1b" not in text
        assert "\x1b" not in style
    # Colored fragments exist (the preview's own styles).
    assert any(
        style and (style.startswith("#") or style.startswith("ansi"))
        for style, _text in frame
    )
    assert "═══" in to_plain_text(frame)


def test_theme_picker_no_color_preview_is_plain() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("no-color")
    frame_text = "".join(t for _s, t in picker._render())
    assert "no color — plain text only" in frame_text


def test_theme_picker_float_mode_plumbing() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    ticks: list[int] = []
    picker = build_theme_picker(
        "dark",
        on_done=lambda r: None,
        invalidate=lambda: ticks.append(1),
    )
    _trigger(picker, "down")
    assert ticks == [1]  # navigation invalidates the host


def test_theme_picker_render_uses_chrome_theme() -> None:
    """Frame chrome (rows/title) uses the *active* theme palette."""
    from phoson_cli.theme_picker import picker_style, build_theme_picker

    style = picker_style(theme=LIGHT)
    assert style is not None
    build_theme_picker("dark", theme=LIGHT)  # constructs without error


# ─── /theme command ──────────────────────────────────────────────────────────


class DummyThemeRenderer:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class DummyThemeRepl:
    def __init__(self, theme: "object" = NO_COLOR) -> None:
        self.theme = theme
        self.config = SimpleNamespace(theme="no-color", provider="ollama")
        self.renderer = DummyThemeRenderer()
        self.applied: list = []

    def apply_theme(self, theme) -> None:
        self.applied.append(theme)
        self.theme = theme


@pytest.fixture(autouse=True)
def _no_real_save(monkeypatch):
    saved: list[object] = []
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kwargs: saved.append((config, kwargs)),
    )
    return saved


@pytest.mark.asyncio
async def test_cmd_theme_opens_picker_and_applies(monkeypatch, _no_real_save) -> None:
    from phoson_cli.commands import Command, CommandHandler
    from phoson_cli.theme_picker import ThemePickerResult

    monkeypatch.setattr(
        "phoson_cli.commands.pick_theme",
        AsyncMock(return_value=ThemePickerResult(theme_name="light")),
    )
    repl = DummyThemeRepl()
    handler = CommandHandler(repl)

    assert await handler.handle(Command(name="/theme", args=""))

    assert repl.applied == [LIGHT]
    assert repl.config.theme == "light"
    assert _no_real_save[-1][1] == {"only_fields": {"theme"}}
    assert "Theme → light" in repl.renderer.infos[-1]


@pytest.mark.asyncio
async def test_cmd_theme_picker_cancel() -> None:
    from phoson_cli.commands import Command, CommandHandler
    from phoson_cli.theme_picker import ThemePickerResult

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "phoson_cli.commands.pick_theme",
        AsyncMock(return_value=ThemePickerResult(cancelled=True)),
    )
    try:
        repl = DummyThemeRepl()
        handler = CommandHandler(repl)
        assert await handler.handle(Command(name="/theme", args=""))
        assert "Cancelled." in repl.renderer.infos[-1]
        assert repl.applied == []
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_cmd_theme_explicit_arg(monkeypatch) -> None:
    from phoson_cli.commands import Command, CommandHandler

    pick = AsyncMock()
    monkeypatch.setattr("phoson_cli.commands.pick_theme", pick)
    repl = DummyThemeRepl()
    handler = CommandHandler(repl)

    assert await handler.handle(Command(name="/theme", args="ANSI"))

    pick.assert_not_awaited()
    assert repl.applied == [ANSI]
    assert repl.config.theme == "ansi"
    assert "Theme → ansi" in repl.renderer.infos[-1]


@pytest.mark.asyncio
async def test_cmd_theme_unknown_name(monkeypatch, _no_real_save) -> None:
    from phoson_cli.commands import Command, CommandHandler

    monkeypatch.setattr("phoson_cli.commands.pick_theme", AsyncMock())
    repl = DummyThemeRepl()
    handler = CommandHandler(repl)

    assert await handler.handle(Command(name="/theme", args="neon"))

    assert "Unknown theme" in repl.renderer.errors[-1]
    assert repl.applied == []
    assert _no_real_save == []


@pytest.mark.asyncio
async def test_cmd_theme_list(monkeypatch) -> None:
    from phoson_cli.commands import Command, CommandHandler

    monkeypatch.setattr("phoson_cli.commands.pick_theme", AsyncMock())
    repl = DummyThemeRepl(theme=DARK)
    handler = CommandHandler(repl)

    assert await handler.handle(Command(name="/theme", args="list"))

    listing = "\n".join(repl.renderer.infos)
    for name in ("dark", "light", "ansi", "no-color"):
        assert name in listing
    assert "* dark" in listing
    assert repl.applied == []


# ─── PhosonRepl.apply_theme ──────────────────────────────────────────────────


def test_repl_apply_theme_repoints_consumers(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from phoson_cli.repl import PhosonRepl

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "phoson_cli.controller.build_chat",
            lambda config: MagicMock(),
        )
        repl = PhosonRepl(PhosonConfig(provider="ollama"))
        assert repl.theme is DARK

        repl.apply_theme(LIGHT)

    assert repl.theme is LIGHT
    assert repl.renderer.theme is LIGHT
    assert repl.renderer._subagent_spinner._theme is LIGHT


# ─── PhosonApp.apply_theme ───────────────────────────────────────────────────


def test_app_apply_theme_recolors_everything(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from phoson_cli.fullscreen.app import PhosonApp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("phoson_cli.controller.build_chat", lambda config: MagicMock())
        app = PhosonApp(PhosonConfig(provider="ollama"))
        original_banner = app._banner_block
        assert app.theme is DARK

        app.apply_theme(LIGHT)

    assert app.theme is LIGHT
    assert app.sink.theme is LIGHT
    assert app.repl.theme is LIGHT
    # The banner block is re-rendered in place (same position, new object).
    assert app.sink.blocks[0] is not original_banner
    assert app.sink.blocks[0] is app._banner_block
    # ANSI cache dropped so the chat pane repaints.
    assert app._block_ansi_cache._width == 0


# ─── FullScreenCommandHost.pick_theme ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fullscreen_host_pick_theme_float() -> None:
    from phoson_cli.theme_picker import ThemePickerResult
    from phoson_cli.fullscreen.command_host import FullScreenCommandHost

    app = MagicMock()
    app.theme = DARK
    app.run_float_picker = AsyncMock(return_value=ThemePickerResult(theme_name="light"))

    host = FullScreenCommandHost(app)
    result = await host.pick_theme("dark", detected_theme="light")

    assert result.theme_name == "light"
    app.run_float_picker.assert_awaited_once()
    picker_arg = app.run_float_picker.await_args.args[0]
    # The picker was built with the app's theme and the detected tier.
    frame_text = "".join(t for _s, t in picker_arg._render())
    assert "(detected)" in frame_text


# ─── first-run suggestion (__main__) ─────────────────────────────────────────


def _patch_suggestion_env(
    monkeypatch, *, persisted: bool, detected: bool | None
) -> None:
    monkeypatch.setattr(
        "phoson_cli.config.has_persisted_theme", lambda *a, **k: persisted
    )
    monkeypatch.setattr(
        "phoson_cli.terminal_theme.detect_terminal_theme", lambda: detected
    )
    monkeypatch.setattr("phoson_cli.__main__.save_config", lambda config, **kw: "saved")


def test_suggestion_skips_when_flag_given(monkeypatch) -> None:
    from phoson_cli import __main__ as main_mod

    _patch_suggestion_env(monkeypatch, persisted=False, detected=True)

    def _no_input(prompt=""):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", _no_input)
    options = main_mod.CliOptions(theme="ansi")
    config = PhosonConfig(provider="ollama")
    main_mod._maybe_offer_theme_suggestion(config, options)
    assert config.theme == "dark"


def test_suggestion_skips_when_persisted(monkeypatch) -> None:
    from phoson_cli import __main__ as main_mod

    _patch_suggestion_env(monkeypatch, persisted=True, detected=True)

    def _no_input(prompt=""):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", _no_input)
    config = PhosonConfig(provider="ollama")
    main_mod._maybe_offer_theme_suggestion(config, main_mod.CliOptions())
    assert config.theme == "dark"


def test_suggestion_skips_when_terminal_unknown(monkeypatch) -> None:
    from phoson_cli import __main__ as main_mod

    _patch_suggestion_env(monkeypatch, persisted=False, detected=None)

    def _no_input(prompt=""):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", _no_input)
    config = PhosonConfig(provider="ollama")
    main_mod._maybe_offer_theme_suggestion(config, main_mod.CliOptions())
    assert config.theme == "dark"


@pytest.mark.asyncio
async def test_suggestion_accepts_and_saves(monkeypatch) -> None:
    from phoson_cli import __main__ as main_mod

    _patch_suggestion_env(monkeypatch, persisted=False, detected=True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "\n")
    config = PhosonConfig(provider="ollama")

    main_mod._maybe_offer_theme_suggestion(config, main_mod.CliOptions())

    assert config.theme == "light"


@pytest.mark.asyncio
async def test_suggestion_declines(monkeypatch) -> None:
    from phoson_cli import __main__ as main_mod

    _patch_suggestion_env(monkeypatch, persisted=False, detected=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n\n")
    config = PhosonConfig(provider="ollama")

    main_mod._maybe_offer_theme_suggestion(config, main_mod.CliOptions())

    assert config.theme == "dark"


@pytest.mark.asyncio
async def test_suggestion_eof_is_silent(monkeypatch) -> None:
    from phoson_cli import __main__ as main_mod

    _patch_suggestion_env(monkeypatch, persisted=False, detected=True)

    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    config = PhosonConfig(provider="ollama")
    main_mod._maybe_offer_theme_suggestion(config, main_mod.CliOptions())
    assert config.theme == "dark"


# ─── E2E: full-screen /theme through the real app shell ──────────────────────


@pytest.mark.asyncio
async def test_tui_theme_explicit_arg_recolors_app(tmp_path, monkeypatch) -> None:
    """/theme <tier> dispatched through the app re-colors it and persists."""
    from phoson_cli.fullscreen.app import PhosonApp

    saved: list = []
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kw: saved.append((config.theme, kw)),
    )
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        app = PhosonApp(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=tmp_path / "history.txt",
            )
        )
        original_banner = app._banner_block

        app._prompt_input.text = "/theme light"
        app.submit()
        assert app._run_task is not None
        await app._run_task
        await asyncio.sleep(0)  # flush the follow-up invalidate

    assert app.theme is LIGHT
    assert app.repl.theme is LIGHT
    assert app.sink.theme is LIGHT
    # The banner was re-rendered in place (same slot, new object).
    assert app.sink.blocks[0] is app._banner_block
    assert app._banner_block is not original_banner
    assert saved == [("light", {"only_fields": {"theme"}})]
    assert app.app.style is not None
    # The "Theme → light" notification landed in the transcript.
    assert any("Theme → light" in str(b) for b in app.sink.blocks)


@pytest.mark.asyncio
async def test_tui_theme_picker_flow_selects_via_float(tmp_path, monkeypatch) -> None:
    """Bare /theme hosts the Float picker; a confirmed pick re-colors."""
    from phoson_cli.theme_picker import ThemePickerResult
    from phoson_cli.fullscreen.app import PhosonApp

    monkeypatch.setattr("phoson_cli.commands.save_config", lambda config, **kw: None)
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        app = PhosonApp(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=tmp_path / "history.txt",
            )
        )
        assert app.theme is DARK

        mock_float = AsyncMock(return_value=ThemePickerResult(theme_name="light"))
        monkeypatch.setattr(app, "run_float_picker", mock_float)

        app._prompt_input.text = "/theme"
        app.submit()
        assert app._run_task is not None
        await app._run_task

        mock_float.assert_awaited_once()
        # The host built a REAL theme picker (float mode), current=dark.
        host_picker = mock_float.call_args.args[0]
        frame = "".join(t for _s, t in host_picker._render())
        assert "current" in frame and "▸  1  dark" in frame

    assert app.theme is LIGHT
    assert app.sink.theme is LIGHT


@pytest.mark.asyncio
async def test_tui_theme_picker_escape_keeps_theme(tmp_path, monkeypatch) -> None:
    """A cancelled /theme float changes nothing and saves nothing."""
    from phoson_cli.theme_picker import ThemePickerResult
    from phoson_cli.fullscreen.app import PhosonApp

    saved: list = []
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kw: saved.append(config.theme),
    )
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        app = PhosonApp(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=tmp_path / "history.txt",
            )
        )
        assert app.theme is DARK

        monkeypatch.setattr(
            app,
            "run_float_picker",
            AsyncMock(return_value=ThemePickerResult(cancelled=True)),
        )

        app._prompt_input.text = "/theme"
        app.submit()
        assert app._run_task is not None
        await app._run_task

    assert app.theme is DARK
    assert saved == []
    assert any("Cancelled." in str(b) for b in app.sink.blocks)


# ─── setup wizard theme question ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wizard_theme_prompt_uses_detection(monkeypatch) -> None:
    from phoson_cli.installer import SetupWizard

    wizard = SetupWizard.__new__(SetupWizard)
    wizard.console = MagicMock()
    wizard.theme = DARK
    monkeypatch.setattr("phoson_cli.terminal_theme.detect_terminal_theme", lambda: True)

    async def _fake_prompt(label, default=None):
        assert label == "Theme"
        assert default == "light"  # detection says light terminal
        return "ansi"

    monkeypatch.setattr(wizard, "_prompt_text", _fake_prompt)
    assert await wizard._pick_theme() == "ansi"


@pytest.mark.asyncio
async def test_wizard_theme_prompt_defaults_on_empty(monkeypatch) -> None:
    from phoson_cli.installer import SetupWizard

    wizard = SetupWizard.__new__(SetupWizard)
    wizard.console = MagicMock()
    wizard.theme = DARK
    monkeypatch.setattr("phoson_cli.terminal_theme.detect_terminal_theme", lambda: None)

    async def _fake_prompt(label, default=None):
        assert default == "dark"
        return ""

    monkeypatch.setattr(wizard, "_prompt_text", _fake_prompt)
    assert await wizard._pick_theme() == "dark"


@pytest.mark.asyncio
async def test_wizard_theme_unknown_falls_back(monkeypatch) -> None:
    from phoson_cli.installer import SetupWizard

    wizard = SetupWizard.__new__(SetupWizard)
    wizard.console = MagicMock()
    wizard.theme = DARK
    monkeypatch.setattr(
        "phoson_cli.terminal_theme.detect_terminal_theme", lambda: False
    )

    async def _fake_prompt(label, default=None):
        return "neon"

    monkeypatch.setattr(wizard, "_prompt_text", _fake_prompt)
    assert await wizard._pick_theme() == "dark"
    wizard.console.print.assert_called()
