"""Unit tests for the full-screen copy mode (IMPROVEMENTS.md G3, #57).

Covers, in dependency order:

1. The pure range math in ``phoson_cli.fullscreen.copy_range`` — same-line and
   multi-line selection, order independence, clamping, page stepping, the
   ANSI→plain-lines flattening and the reverse-video highlight pass.
2. The clipboard *write* side (``write_clipboard_text`` and its platform
   command selection) — the inverse of the existing read helpers.
3. ``PhosonApp`` copy-mode behaviour — entering/leaving the mode, the
   anchor/cursor seeding, arrow/page/home/end navigation, the yank action,
   the key-map wiring (``f2`` default + remap), and the pane highlight +
   footer swap while the mode is active.
"""

import io
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import (
    KNOWN_KEY_ACTIONS,
    PhosonConfig,
    load_key_bindings,
)
from phoson_cli.fullscreen import copy_range
from phoson_cli.fullscreen.keys import (
    DEFAULT_KEY_BINDINGS,
    listing_for_config,
    resolve_key_bindings,
)


def _app_for(tmp_path, **config_kwargs):
    """A bare PhosonApp (mocked chat client) with optional config extras."""
    from phoson_cli.fullscreen.app import PhosonApp

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            **config_kwargs,
        )
        return PhosonApp(config)


def _seed_chat(app, *lines: str) -> None:
    """Seed the sink with exactly ``lines`` (banner removed) and re-render.

    After this, ``app._chat_plain_lines`` is exactly ``list(lines)`` (each
    seeded short string renders as a single wrapped row) and scroll is 0, so
    the range math and navigation can be asserted against a known, short
    transcript.
    """
    app.sink.blocks = list(lines)
    app._auto_scroll = True
    app._chat_scroll_top = 0
    app.sink.dirty = True
    app._last_width = 0
    app._render_chat()


def _composer_focus_window(app):
    """The inner Window the composer's TextArea focuses.

    Focusing the ``TextArea`` container routes to its internal
    ``BufferControl``/``Window``; ``layout.current_window`` is that Window,
    never the ``TextArea`` itself.
    """
    return app._prompt_input.window


# ── copy_range: range_text ────────────────────────────────────────────────────


def test_range_text_same_line() -> None:
    lines = ["hello world"]
    assert (
        copy_range.range_text(lines, copy_range.Pos(0, 0), copy_range.Pos(0, 5))
        == "hello"
    )
    assert (
        copy_range.range_text(lines, copy_range.Pos(0, 6), copy_range.Pos(0, 11))
        == "world"
    )
    # Zero-width (anchor == cursor) selects nothing.
    assert (
        copy_range.range_text(lines, copy_range.Pos(0, 3), copy_range.Pos(0, 3)) == ""
    )


def test_range_text_multi_line_grabs_rest_then_prefix() -> None:
    lines = ["alpha", "beta gamma", "delta"]
    # "alpha"[2:] = "pha"; "delta"[:3] = "del"; full middle line in between.
    text = copy_range.range_text(lines, copy_range.Pos(0, 2), copy_range.Pos(2, 3))
    assert text == "pha\nbeta gamma\ndel"


def test_range_text_is_order_independent() -> None:
    lines = ["one", "two", "three"]
    a = copy_range.Pos(2, 2)
    b = copy_range.Pos(0, 1)
    assert copy_range.range_text(lines, a, b) == copy_range.range_text(lines, b, a)
    # Sanity: extending downward from line 0 to line 2 grabs a middle line.
    assert "two" in copy_range.range_text(lines, b, a)


def test_range_text_empty_transcript() -> None:
    assert copy_range.range_text([], copy_range.Pos(0, 0), copy_range.Pos(0, 0)) == ""


def test_range_text_clamps_out_of_range_positions() -> None:
    lines = ["short", "longer line here"]
    # Start past the end of line 0, end past the end of line 1.
    text = copy_range.range_text(lines, copy_range.Pos(0, 999), copy_range.Pos(1, 999))
    # start clamps to col=len("short") → tail empty; end clamps to line 1 end.
    assert text == "\nlonger line here"


# ── copy_range: clamp / span / page ───────────────────────────────────────────


def test_clamp_position_snaps_into_range() -> None:
    lines = ["abc", "de"]
    assert copy_range.clamp_position(lines, copy_range.Pos(5, 9)) == copy_range.Pos(
        1, 2
    )
    assert copy_range.clamp_position(lines, copy_range.Pos(-1, -1)) == copy_range.Pos(
        0, 0
    )
    assert copy_range.clamp_position([], copy_range.Pos(3, 3)) == copy_range.Pos(0, 0)


def test_selection_line_span() -> None:
    lines = ["a", "b", "c", "d"]
    assert copy_range.selection_line_span(
        lines, copy_range.Pos(1, 0), copy_range.Pos(3, 2)
    ) == (1, 3)
    # Order-independent.
    assert copy_range.selection_line_span(
        lines, copy_range.Pos(3, 2), copy_range.Pos(1, 0)
    ) == (1, 3)


def test_step_page_moves_a_full_page_to_an_edge() -> None:
    lines = [f"l{i}" for i in range(30)]
    p = copy_range.Pos(5, 3)
    # Forward: land at the start of the destination line.
    fwd = copy_range.step_page(lines, p, 10)
    assert fwd == copy_range.Pos(15, 0)
    # Backward: land at the end of the destination line.
    back = copy_range.step_page(lines, p, -3)
    assert back == copy_range.Pos(2, len(lines[2]))
    # Out of range clamps to the transcript edge.
    assert copy_range.step_page(lines, p, 1000) == copy_range.Pos(29, len(lines[29]))
    assert copy_range.step_page(lines, p, -1000) == copy_range.Pos(0, 0)


# ── copy_range: plain_lines + apply_reverse_highlight ─────────────────────────


def test_plain_lines_counts_newlines_plus_one() -> None:
    fragments = [
        ("", "ab\n"),
        ("", "cd"),
        ("style", "e\nf"),
    ]
    # "ab" | "cde" (the "cd" fragment and the "e" fragment share one row)
    # | "f" → 3 lines. A fragment's first segment continues the current row.
    assert copy_range.plain_lines(fragments) == ["ab", "cde", "f"]


def test_apply_reverse_highlight_wraps_only_selected_rows() -> None:
    fragments = [("", "one\ntwo\nthree\n")]
    out = copy_range.apply_reverse_highlight(fragments, lo=1, hi=1)
    assert "\x1b[7mtwo\x1b[27m" in out
    # The non-selected rows are left untouched (no reverse markers around them).
    assert out.count("\x1b[7m") == 1
    assert "one" in out and "three" in out
    # Re-flattening the highlighted output preserves the row text.
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text

    refilled = copy_range.plain_lines(to_formatted_text(ANSI(out)))
    assert refilled == ["one", "two", "three", ""]


# ── clipboard write side ───────────────────────────────────────────────────────


def test_write_command_picks_platform_tool(monkeypatch) -> None:
    from phoson_cli.fullscreen import clipboard

    def _only(tool: str | None):
        """A fake shutil.which that finds only *tool* (else None)."""
        return lambda name: f"/bin/{tool}" if name == tool else None

    # Wayland wins when its env + tool are present.
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", _only("wl-copy"))
    assert clipboard._write_command() == ["wl-copy"]

    # X11 next (no Wayland display).
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clipboard.shutil, "which", _only("xclip"))
    assert clipboard._write_command() == ["xclip", "-selection", "clipboard"]

    # No display env → only macOS pbcopy applies.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(clipboard, "_is_macos", lambda: True)
    monkeypatch.setattr(clipboard.shutil, "which", _only("pbcopy"))
    assert clipboard._write_command() == ["pbcopy"]

    # Nothing available → None.
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard._write_command() is None


def test_write_clipboard_text_success_and_failure() -> None:
    from phoson_cli.fullscreen import clipboard

    async def _run(command: list[str]) -> bool:
        with (
            patch.object(clipboard, "_write_command", return_value=command),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_fake_proc(0)),
            ),
        ):
            return await clipboard.write_clipboard_text("hi")

    assert asyncio.run(_run(["xclip", "-selection", "clipboard", "-o"])) is True

    async def _run_fail() -> bool:
        with (
            patch.object(clipboard, "_write_command", return_value=["xclip"]),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_fake_proc(1)),
            ),
        ):
            return await clipboard.write_clipboard_text("hi")

    assert asyncio.run(_run_fail()) is False

    # No tool at all → False, no subprocess.
    async def _run_notool() -> bool:
        with patch.object(clipboard, "_write_command", return_value=None):
            return await clipboard.write_clipboard_text("hi")

    assert asyncio.run(_run_notool()) is False


def test_clipboard_write_available_and_hint(monkeypatch) -> None:
    from phoson_cli.fullscreen import clipboard

    monkeypatch.setattr(clipboard, "_write_command", lambda: ["xclip"])
    assert clipboard.clipboard_write_available() is True
    assert clipboard.clipboard_write_hint() is None

    monkeypatch.setattr(clipboard, "_write_command", lambda: None)
    assert clipboard.clipboard_write_available() is False
    hint = clipboard.clipboard_write_hint()
    assert hint is not None
    assert "xclip" in hint or "wl-clipboard" in hint or "pbcopy" in hint


# ── clipboard write side: OSC 52 fallback (G3 follow-up) ──────────────────────


def test_osc52_sequence_is_well_formed() -> None:
    import base64

    from phoson_cli.fullscreen import clipboard

    seq = clipboard.osc52_sequence("Hello, world!")
    encoded = base64.b64encode(b"Hello, world!").decode("ascii")
    # ESC ] 52 ; c ; <base64> ST (ST = ESC \)
    assert seq == f"\x1b]52;c;{encoded}\x1b\\"
    # Multi-line / unicode round-trips through base64 intact.
    seq2 = clipboard.osc52_sequence("línea uno\nlínea dos")
    payload = seq2.split(";")[2].rstrip("\x1b\\")
    assert base64.b64decode(payload) == "línea uno\nlínea dos".encode()


def test_osc52_enabled_resolution() -> None:
    from phoson_cli.fullscreen import clipboard

    assert clipboard.osc52_enabled("on") is True
    assert clipboard.osc52_enabled("OFF") is False
    # "auto" / None / unknown delegate to the environment detection.
    for value in ("auto", None, "bogus"):
        assert clipboard.osc52_enabled(value) is clipboard.osc52_supported()


def _clean_osc52_env(monkeypatch) -> None:
    for var in (
        "TERM",
        "TERM_PROGRAM",
        "KITTY_WINDOW_ID",
        "ALACRITTY_INSTANCE_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_osc52_detection_by_term_program(monkeypatch) -> None:
    from phoson_cli.fullscreen import clipboard

    _clean_osc52_env(monkeypatch)
    supported = {
        "kitty": True,
        "WezTerm": True,
        "iTerm.app": True,
        "WindowsTerminal": True,
        "ghostty": True,
        "vscode": False,  # xterm.js w/o the clipboard addon
        "Apple_Terminal": False,  # macOS Terminal.app: no OSC 52
        "": False,
    }
    for program, expected in supported.items():
        monkeypatch.setenv("TERM_PROGRAM", program)
        assert clipboard.osc52_supported() is expected, program


def test_osc52_detection_by_term(monkeypatch) -> None:
    from phoson_cli.fullscreen import clipboard

    _clean_osc52_env(monkeypatch)
    supported = {
        "xterm-kitty": True,  # kitty's $TERM
        "xterm-ghostty": True,
        "alacritty": True,
        "foot": True,
        "contour": True,
        "st-256color": True,  # st's $TERM
        "xterm-256color": False,  # plain xterm: opt-in, not recognized
        "screen-256color": False,
        "linux": False,
    }
    for term, expected in supported.items():
        monkeypatch.setenv("TERM", term)
        assert clipboard.osc52_supported() is expected, term


def test_osc52_detection_by_terminal_env_var(monkeypatch) -> None:
    from phoson_cli.fullscreen import clipboard

    _clean_osc52_env(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    assert clipboard.osc52_supported() is True
    monkeypatch.delenv("KITTY_WINDOW_ID")
    monkeypatch.setenv("ALACRITTY_INSTANCE_ID", "123")
    assert clipboard.osc52_supported() is True


def test_write_clipboard_osc52_no_tty_returns_false() -> None:
    import os

    from phoson_cli.fullscreen import clipboard

    # No controlling tty (piped/daemonized run) -> False, no crash.
    with patch.object(os, "open", side_effect=OSError("no tty")):
        assert clipboard.write_clipboard_osc52("hi") is False
    # Empty text is a no-op.
    assert clipboard.write_clipboard_osc52("") is False
    # A failing write() also reports False.
    with (
        patch.object(os, "open", return_value=3),
        patch.object(os, "write", side_effect=OSError("broken pipe")),
        patch.object(os, "close"),
    ):
        assert clipboard.write_clipboard_osc52("hi") is False


def test_write_clipboard_osc52_sends_the_sequence(monkeypatch) -> None:
    import os
    import base64

    from phoson_cli.fullscreen import clipboard

    written = {}

    def _fake_write(fd, data):
        written["data"] = data
        return len(data)

    monkeypatch.setattr(os, "open", lambda *a, **k: 3)
    monkeypatch.setattr(os, "write", _fake_write)
    monkeypatch.setattr(os, "close", lambda fd: None)
    assert clipboard.write_clipboard_osc52("hello") is True
    expected = base64.b64encode(b"hello").decode("ascii")
    assert written["data"] == f"\x1b]52;c;{expected}\x1b\\".encode()


def test_yank_falls_back_to_osc52_when_no_tool(tmp_path) -> None:
    """No platform tool + OSC 52 enabled -> the sequence is used, not a warn."""
    app = _app_for(tmp_path)
    _seed_chat(app, "hello world")
    app.enter_copy_mode()
    app._copy_anchor = copy_range.Pos(0, 0)
    app._copy_cursor = copy_range.Pos(0, 5)

    sent = {}

    def _fake_osc52(text):
        sent["text"] = text
        return True

    async def _no_tool(text):
        return False

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_no_tool),
        patch("phoson_cli.fullscreen.app.osc52_enabled", return_value=True),
        patch("phoson_cli.fullscreen.app.write_clipboard_osc52", new=_fake_osc52),
        _run_bg_inline(app),
    ):
        app.copy_copy()

    assert sent.get("text") == "hello"  # exactly the selected range
    # The success notice mentions the OSC 52 path, not a failure.
    assert any("OSC 52" in n for n in _block_texts(app))
    assert app._copy_active is False


def test_yank_warn_mentions_osc52_when_disabled(tmp_path) -> None:
    """No tool + OSC 52 off -> the old warn, plus the OSC 52 tip."""
    app = _app_for(tmp_path)
    _seed_chat(app, "hello world")
    app.enter_copy_mode()
    app._copy_anchor = copy_range.Pos(0, 0)
    app._copy_cursor = copy_range.Pos(0, 5)

    async def _no_tool(text):
        return False

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_no_tool),
        patch("phoson_cli.fullscreen.app.osc52_enabled", return_value=False),
        patch(
            "phoson_cli.fullscreen.app.clipboard_write_hint",
            return_value="install xclip for X11 clipboard writes",
        ),
        patch("phoson_cli.fullscreen.app.write_clipboard_osc52") as mock_osc52,
        _run_bg_inline(app),
    ):
        app.copy_copy()

    mock_osc52.assert_not_called()
    notices = " ".join(_block_texts(app))
    assert "Could not copy" in notices
    assert 'clipboard_osc52 = "on"' in notices


def _fake_proc(returncode: int):
    proc = MagicMock()
    proc.returncode = returncode

    async def _communicate(data=None):
        return (b"", b"")

    proc.communicate = _communicate
    return proc


# ── key-map wiring (E6-style table) ───────────────────────────────────────────


def test_default_key_map_has_copy_mode() -> None:
    assert DEFAULT_KEY_BINDINGS["copy_mode"] == ["f2"]
    # The set of known actions still matches the built-in map exactly.
    assert set(DEFAULT_KEY_BINDINGS) == set(KNOWN_KEY_ACTIONS)
    assert "copy_mode" in KNOWN_KEY_ACTIONS


def test_listing_shows_copy_mode() -> None:
    display = dict(listing_for_config(None))
    assert display["copy_mode"] == "F2"


def test_copy_mode_remap_and_unbind_work_like_any_action() -> None:
    resolved = resolve_key_bindings(overrides={"copy_mode": ["f5"]})
    assert resolved["copy_mode"] == ["f5"]
    assert "f2" not in [s for s in resolved["copy_mode"]]
    display = dict(listing_for_config(PhosonConfig(key_bindings={"copy_mode": ["f5"]})))
    assert display["copy_mode"] == "F5"
    # Unbindable.
    assert resolve_key_bindings(overrides={"copy_mode": []})["copy_mode"] == []


def test_copy_mode_config_loads(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    (home / ".phoson" / "config.toml").write_text(
        '[defaults]\n\n[keys]\ncopy_mode = "f5"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    assert load_key_bindings() == {"copy_mode": ["f5"]}


# ── /copy command ─────────────────────────────────────────────────────────────


def test_copy_command_registered_and_categorized() -> None:
    from phoson_cli.commands import COMMANDS, COMMAND_SPECS, get_grouped_command_help

    assert "/copy" in COMMANDS
    spec = next(s for s in COMMAND_SPECS if "/copy" in s.names)
    assert spec.method == "_cmd_copy"
    grouped = dict(get_grouped_command_help())
    assert "/copy" in {name for name, _ in grouped["Config & System"]}


def test_copy_command_full_screen_enters_copy_mode(tmp_path) -> None:
    """``/copy`` dispatches through the full-screen host to ``enter_copy_mode``."""
    import asyncio

    from phoson_cli.commands import Command, CommandHandler
    from phoson_cli.fullscreen.command_host import FullScreenCommandHost

    app = _app_for(tmp_path)
    _seed_chat(app, "a", "b")
    handler = CommandHandler(app.repl, host=FullScreenCommandHost(app))
    assert "/copy" in handler._dispatch
    asyncio.run(handler.handle(Command(name="/copy", args="")))
    assert app._copy_active is True


def test_copy_command_classic_repl_notices(tmp_path) -> None:
    """The classic front end has no selectable pane → a notice, no crash."""
    import asyncio

    from phoson_cli.commands import Command, CommandHandler

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        from phoson_cli.repl import PhosonRepl

        repl = PhosonRepl(config)
    lines: list[str] = []

    class _Host:
        def print_info(self, m) -> None:
            lines.append(m)

        def start_copy_mode(self) -> None:
            self.print_info("copy-mode-not-available-notice")

    handler = CommandHandler(repl, host=_Host())
    asyncio.run(handler.handle(Command(name="/copy", args="")))
    assert any("copy-mode-not-available-notice" in line for line in lines)


# ── PhosonApp: enter / exit / navigation ──────────────────────────────────────


def test_enter_copy_mode_seeds_anchor_cursor_and_focusses_chat(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "line one", "line two", "line three")

    app.enter_copy_mode()

    assert app._copy_active is True
    assert app.app.layout.current_window is app._chat_window
    # Short transcript (fits the visible pane, scroll 0): anchor at the top,
    # cursor at the bottom-right → the whole visible pane is pre-selected.
    assert app._copy_anchor == copy_range.Pos(0, 0)
    lines = app._chat_plain_lines
    assert app._copy_cursor == copy_range.Pos(len(lines) - 1, len(lines[-1]))
    # The default selection therefore covers the entire seeded transcript.
    text = copy_range.range_text(lines, app._copy_anchor, app._copy_cursor)
    assert "line one" in text and "line three" in text


def test_enter_copy_mode_blocked_while_run_in_flight(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "a", "b")
    blocks_before = len(app.sink.blocks)

    with patch.object(app, "_is_run_in_flight", return_value=True):
        app.enter_copy_mode()

    assert app._copy_active is False
    assert len(app.sink.blocks) == blocks_before + 1  # one notice


def test_copy_lines_recomputes_when_cache_empty(tmp_path) -> None:
    """``_copy_lines`` falls back to re-parsing the base when the cache is empty.

    An empty sink renders a single placeholder row, so copy mode can still be
    entered (harmlessly) and the recompute path runs — it must not crash.
    """
    app = _app_for(tmp_path)
    app.sink.blocks.clear()
    app.sink.dirty = True
    app._last_width = 0
    app._render_chat()

    # Force the recompute path.
    app._chat_plain_lines = []
    lines = app._copy_lines()
    assert len(lines) >= 1  # at least the placeholder row

    app.enter_copy_mode()
    assert app._copy_active is True
    app.copy_cancel()
    assert app._copy_active is False


def test_copy_navigation_arrows_move_cursor(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "aaaa", "bbbb", "cccc")
    app.enter_copy_mode()
    lines = app._chat_plain_lines
    # Start from a known cursor in the middle of line 1.
    app._copy_cursor = copy_range.Pos(1, 1)

    app.copy_move_right()
    assert app._copy_cursor == copy_range.Pos(1, 2)
    app.copy_move_left()
    assert app._copy_cursor == copy_range.Pos(1, 1)
    app.copy_move_left()
    assert app._copy_cursor == copy_range.Pos(1, 0)
    # Left at col 0 wraps to the end of the previous line.
    app.copy_move_left()
    assert app._copy_cursor == copy_range.Pos(0, len(lines[0]))
    # Right from end of line 0 wraps to the start of line 1.
    app._copy_cursor = copy_range.Pos(0, len(lines[0]))
    app.copy_move_right()
    assert app._copy_cursor == copy_range.Pos(1, 0)
    app.copy_move_down()
    assert app._copy_cursor.line == 2
    app.copy_move_up()
    assert app._copy_cursor.line == 1


def test_copy_navigation_home_end_and_page(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "aaaa", "bbbb", "cccc", "dddd")
    app.enter_copy_mode()
    lines = app._chat_plain_lines
    app._copy_cursor = copy_range.Pos(1, 2)

    app._copy_move_to_line_start()
    assert app._copy_cursor == copy_range.Pos(1, 0)
    app._copy_move_to_line_end()
    assert app._copy_cursor == copy_range.Pos(1, len(lines[1]))

    # Page down jumps a full visible page (>= len of a short transcript),
    # clamping to the last line's end.
    app.copy_page_down()
    assert app._copy_cursor == copy_range.Pos(len(lines) - 1, len(lines[-1]))
    app.copy_page_up()
    assert app._copy_cursor == copy_range.Pos(0, 0)


def test_copy_copy_yanks_and_exits(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "hello world", "second line")
    app.enter_copy_mode()
    # Select exactly "hello" (line 0, cols 0..5).
    app._copy_anchor = copy_range.Pos(0, 0)
    app._copy_cursor = copy_range.Pos(0, 5)

    copied = {}

    async def _fake_write(text):
        copied["text"] = text
        return True

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write),
        _run_bg_inline(app),
    ):
        app.copy_copy()

    assert copied.get("text") == "hello"
    assert app._copy_active is False
    assert app.app.layout.current_window is app._prompt_input.window


def test_copy_copy_empty_selection_warns_and_exits(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "hello", "world")
    app.enter_copy_mode()
    blocks_before = len(app.sink.blocks)
    # Anchor == cursor → empty selection.
    app._copy_anchor = copy_range.Pos(0, 2)
    app._copy_cursor = copy_range.Pos(0, 2)

    with patch("phoson_cli.fullscreen.app.write_clipboard_text") as mock_w:
        app.copy_copy()

    mock_w.assert_not_called()
    assert len(app.sink.blocks) == blocks_before + 1  # one warn notice
    assert app._copy_active is False


def test_copy_write_failure_notifies(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "hello world")
    app.enter_copy_mode()
    app._copy_anchor = copy_range.Pos(0, 0)
    app._copy_cursor = copy_range.Pos(0, 5)
    blocks_before = len(app.sink.blocks)

    async def _fail(text):
        return False

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fail),
        # Force OSC 52 off so this test is deterministic no matter which
        # terminal (or its env vars) the suite happens to run under — the
        # "no tool at all" warn is what we're asserting here.
        patch("phoson_cli.fullscreen.app.osc52_enabled", return_value=False),
        patch(
            "phoson_cli.fullscreen.app.clipboard_write_hint",
            return_value="install xclip for X11 clipboard writes",
        ),
        _run_bg_inline(app),
    ):
        app.copy_copy()

    assert len(app.sink.blocks) == blocks_before + 1  # warn notice
    assert app._copy_active is False


def test_copy_cancel_exits_without_copied(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "hello world")
    app.enter_copy_mode()

    with patch("phoson_cli.fullscreen.app.write_clipboard_text") as mock_w:
        app.copy_cancel()

    mock_w.assert_not_called()
    assert app._copy_active is False
    assert app.app.layout.current_window is app._prompt_input.window


# ── rendering: highlight + footer swap ────────────────────────────────────────


def test_render_chat_highlights_selection_only_when_active(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "alpha", "beta", "gamma")

    base = app._render_chat().value
    assert "\x1b[7m" not in base

    app._copy_active = True
    app._copy_anchor = copy_range.Pos(0, 0)
    app._copy_cursor = copy_range.Pos(1, 4)
    highlighted = app._render_chat().value
    assert "\x1b[7m" in highlighted  # selected rows are reversed
    # Exit → the plain view returns with no reverse markers.
    app._copy_active = False
    app._render_chat()
    assert "\x1b[7m" not in app._render_chat().value


def test_footer_swaps_in_copy_mode(tmp_path) -> None:
    app = _app_for(tmp_path)
    _seed_chat(app, "a", "b")
    from prompt_toolkit.formatted_text import to_plain_text

    normal = to_plain_text(app._get_footer_text())
    assert "[Enter] Send" in normal

    app._copy_active = True
    copy_hint = to_plain_text(app._get_footer_text())
    assert "Enter" in copy_hint and "Copy" in copy_hint
    assert "[Enter] Send" not in copy_hint

    app._copy_active = False
    assert to_plain_text(app._get_footer_text()) == normal


def test_copy_mode_does_not_steer_normal_composer(tmp_path) -> None:
    """Outside copy mode the entry key is bound but inert on the composer."""
    app = _app_for(tmp_path)
    _seed_chat(app, "a", "b")
    # The F2 binding exists at the app level.
    bound = {
        tuple(getattr(k, "value", str(k)) for k in b.keys)
        for b in app.app.key_bindings.bindings
    }
    assert ("f2",) in bound
    # Entering and leaving restores composer focus.
    app.enter_copy_mode()
    assert app.app.layout.current_window is app._chat_window
    app.copy_cancel()
    assert app.app.layout.current_window is app._prompt_input.window


# ── real keystroke routing (PipeInput) ────────────────────────────────────────
# These send raw bytes through the real VT100 input layer (same pattern as the
# G1 rewind tests): F2 opens copy mode, arrows extend the range, Enter yanks
# and exits, a lone Esc cancels. This is the highest-risk integration point —
# the copy bindings must actually own the keys while the chat window is focused
# and the base bindings are gated off.

F2 = "\x1bOQ"  # xterm F2


@pytest.mark.asyncio
async def test_pipe_input_f2_opens_copy_mode_and_enter_copies(tmp_path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    _seed_chat(app, "one line of chat", "second line")
    copied = {}

    async def _fake_write(text):
        copied["text"] = text
        return True

    with patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write):
        with create_pipe_input() as pipe:
            app.app.input = pipe
            app.app.output = DummyOutput()

            async def drive():
                await asyncio.sleep(0.02)
                pipe.send_text(F2)  # open copy mode
                await asyncio.sleep(0.1)
                pipe.send_text("\x1b[D")  # ← extend the range left
                await asyncio.sleep(0.08)
                pipe.send_text("\r")  # Enter: copy + exit
                await asyncio.sleep(0.1)
                pipe.send_text("\x03")  # quit

            asyncio.create_task(drive())
            await app.app.run_async()

    assert app._copy_active is False  # exited after the copy
    assert copied.get("text", "").strip()  # something was actually yanked
    assert app.app.layout.current_window is app._prompt_input.window


@pytest.mark.asyncio
async def test_pipe_input_f2_then_esc_cancels_copy_mode(tmp_path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    _seed_chat(app, "hello", "world")

    with patch("phoson_cli.fullscreen.app.write_clipboard_text") as mock_w:
        with create_pipe_input() as pipe:
            app.app.input = pipe
            app.app.output = DummyOutput()

            async def drive():
                await asyncio.sleep(0.02)
                pipe.send_text(F2)  # open copy mode
                await asyncio.sleep(0.1)
                assert app._copy_active is True
                pipe.send_text("\x1b")  # Esc: cancel
                await asyncio.sleep(0.1)
                pipe.send_text("\x03")  # quit

            asyncio.create_task(drive())
            await app.app.run_async()

    mock_w.assert_not_called()
    assert app._copy_active is False
    assert app.app.layout.current_window is app._prompt_input.window


@pytest.mark.asyncio
async def test_pipe_input_typing_not_steered_by_copy_entry(tmp_path) -> None:
    """In normal mode the F2 *entry* binding is present but inert on typed text.

    Guards the regression that reserving the entry key at the app level does
    not steal ordinary typing from the composer.
    """
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    _seed_chat(app, "x", "y")

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive():
            await asyncio.sleep(0.02)
            pipe.send_text("hi")  # plain typing stays in the composer
            await asyncio.sleep(0.08)
            pipe.send_text("\x03")  # quit

        asyncio.create_task(drive())
        await app.app.run_async()

    assert app._copy_active is False
    assert app._prompt_input.buffer.text == "hi"


# ── copy_range: word_bounds (mouse double-click) ──────────────────────────────


def test_word_bounds_selects_whole_word_under_cursor() -> None:
    assert copy_range.word_bounds("hello world", 2) == (0, 5)
    assert copy_range.word_bounds("hello world", 7) == (6, 11)
    # A click right at the word edge grabs the word.
    assert copy_range.word_bounds("hello world", 5) == (0, 5)
    assert copy_range.word_bounds("hello world", 6) == (6, 11)


def test_word_bounds_on_whitespace_prefers_next_word() -> None:
    # A cell on a word's trailing edge belongs to that word...
    assert copy_range.word_bounds("hello world", 5) == (0, 5)
    # ...but a leading space selects the next word to the right...
    assert copy_range.word_bounds(" hello", 0) == (1, 6)
    # ...and the middle of a double gap goes to the next word too.
    assert copy_range.word_bounds("hello  world", 6) == (7, 12)
    # Trailing space past the last word: snaps back to it.
    assert copy_range.word_bounds("hello ", 6) == (0, 5)
    assert copy_range.word_bounds("", 0) == (0, 0)
    assert copy_range.word_bounds("   ", 1) == (0, 0)


def test_word_bounds_paths_and_urls() -> None:
    line = "see /tmp/a-b.c.txt now"
    start, end = copy_range.word_bounds(line, 6)
    assert line[start:end] == "/tmp/a-b.c.txt"


def test_word_bounds_clamps_out_of_range_col() -> None:
    assert copy_range.word_bounds("hello world", 999) == (6, 11)
    assert copy_range.word_bounds("hello world", -1) == (0, 5)


# ── PhosonApp: mouse selection (G3 follow-up) ─────────────────────────────────


def _mouse_event(
    event_type,
    row: int,
    col: int,
    button=None,
    modifiers=frozenset(),
):
    """A MouseEvent in the *content* coordinate space the Window wrapper
    produces (position = Point(x=col, y=row))."""
    from prompt_toolkit.mouse_events import MouseEvent, MouseButton
    from prompt_toolkit.data_structures import Point

    if button is None:
        button = MouseButton.LEFT
    return MouseEvent(
        position=Point(x=col, y=row),
        event_type=event_type,
        button=button,
        modifiers=modifiers,
    )


def test_mouse_click_drag_yanks_and_keeps_selection(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello world", "second line")
    copied = {}

    async def _fake_write(text):
        copied["text"] = text
        return True

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write),
        _run_bg_inline(app),
    ):
        # Press at line 0 col 0, drag to line 1 col 6, release there.
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 0))
        assert app._copy_active is True
        assert app._copy_via_mouse is True
        assert app._copy_anchor == copy_range.Pos(0, 0)
        assert app.app.layout.current_window is app._chat_window
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_MOVE, 1, 3))
        assert app._copy_cursor == copy_range.Pos(1, 3)
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 1, 6))

    # A drag yanks the range on release and stays in copy mode (the
    # selection is still visible, refinable by keyboard).
    assert copied.get("text") == "hello world\nsecond"
    assert app._copy_active is True
    assert app._copy_anchor == copy_range.Pos(0, 0)
    assert app._copy_cursor == copy_range.Pos(1, 6)
    # Esc still exits the (mouse-opened) mode.
    app.copy_cancel()
    assert app._copy_active is False
    assert app._copy_via_mouse is False
    assert app.app.layout.current_window is app._prompt_input.window


def test_mouse_bare_click_enters_and_closes_without_copy(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello world")
    with patch("phoson_cli.fullscreen.app.write_clipboard_text") as mock_w:
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 2))
        assert app._copy_active is True
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 0, 2))
    mock_w.assert_not_called()
    assert app._copy_active is False
    assert app.app.layout.current_window is app._prompt_input.window


def test_mouse_double_click_selects_word_and_stays(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello world")
    copied = {}

    async def _fake_write(text):
        copied["text"] = text
        return True

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write),
        _run_bg_inline(app),
    ):
        # First click: press + release on the same cell → mode closed.
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 7))
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 0, 7))
        assert app._copy_active is False
        # Second click within the double-click window, same cell:
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 7))
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 0, 7))

    # The whole word "world" is selected and the mode stays open.
    assert copied == {}  # a word select does not yank by itself
    assert app._copy_active is True
    assert app._copy_anchor == copy_range.Pos(0, 6)
    assert app._copy_cursor == copy_range.Pos(0, 11)
    # Enter yanks the selected word.
    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write),
        _run_bg_inline(app),
    ):
        app.copy_copy()
    assert copied.get("text") == "world"
    assert app._copy_active is False


def test_mouse_double_click_not_fooled_by_drag_then_click(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello world", "second line")

    async def _fake_write(text):
        return True

    with (
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write),
        _run_bg_inline(app),
    ):
        # Drag from (0,0) to (1,3) — a release on a *different* cell.
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 0))
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_MOVE, 1, 3))
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 1, 3))
        # Immediately click on (1, 3): not a double-click (the previous
        # release was a drag), and the bare click closes the mode.
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 1, 3))
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 1, 3))

    assert app._copy_active is False
    assert app.app.layout.current_window is app._prompt_input.window


def test_mouse_press_ignored_while_run_in_flight(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello")
    with patch.object(app, "_is_run_in_flight", return_value=True):
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 0))
    assert app._copy_active is False
    assert app._mouse_down is False
    # A subsequent release must be a no-op (no selection to finish).
    app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 0, 0))
    assert app._copy_active is False


def test_mouse_press_ignored_while_float_open(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello")
    app._active_float = MagicMock()  # any open Float blocks mouse selection
    try:
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 0))
    finally:
        app._active_float = None
    assert app._copy_active is False


def test_mouse_non_left_button_is_noop(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, "hello")
    result = app._on_chat_mouse(
        _mouse_event(MouseEventType.MOUSE_DOWN, 0, 0, button=MouseButton.RIGHT)
    )
    assert result is NotImplemented
    assert app._copy_active is False


def test_mouse_scroll_passthrough_and_wheel_keeps_working(tmp_path) -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    app = _app_for(tmp_path)
    _seed_chat(app, *([f"row {i}" for i in range(60)]))
    # Make the transcript longer than the pane so the wheel has room.
    app._auto_scroll = False
    app._chat_scroll_top = 0
    app._render_chat()
    max_scroll = app._total_chat_lines - app._get_visible_window_height()
    assert max_scroll > 3

    # The control-level handler passes scroll events through...
    assert (
        app._on_chat_mouse(_mouse_event(MouseEventType.SCROLL_UP, 0, 0))
        is NotImplemented
    )
    # ...and the Window-level fallback performs the actual scroll.
    # From the top, scrolling down moves 3 rows and disables auto-scroll.
    result = app._on_chat_scroll(_mouse_event(MouseEventType.SCROLL_DOWN, 0, 0))
    assert result is None
    assert app._chat_scroll_top == 3
    assert app._auto_scroll is False
    # Scrolling up from a mid position moves 3 rows back.
    app._on_chat_scroll(_mouse_event(MouseEventType.SCROLL_UP, 0, 0))
    assert app._chat_scroll_top == 0
    # Scroll-down all the way to the bottom re-enables auto-scroll.
    app._chat_scroll_top = max_scroll - 2
    app._on_chat_scroll(_mouse_event(MouseEventType.SCROLL_DOWN, 0, 0))
    assert app._chat_scroll_top == max_scroll
    assert app._auto_scroll is True
    # Unknown event types pass through untouched.
    assert (
        app._on_chat_scroll(_mouse_event(MouseEventType.MOUSE_UP, 0, 0))
        is NotImplemented
    )


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _block_texts(app) -> list[str]:
    """Plain text of every transcript block (Rich renderables → str).

    Notices are Rich :class:`rich.text.Text`; other blocks (banner, turns)
    are arbitrary renderables — render those into a throwaway console so the
    helper works for any block type the sink may hold.
    """
    from rich.text import Text
    from rich.console import Console

    out: list[str] = []
    for block in app.sink.blocks:
        if isinstance(block, Text):
            out.append(block.plain)
        else:
            buf = io.StringIO()
            Console(file=buf, width=120, no_color=True).print(block, soft_wrap=True)
            out.append(buf.getvalue().rstrip("\n"))
    return out


def test_mouse_selection_highlight_renders_in_pane(tmp_path) -> None:
    """The reverse-video highlight follows a mouse drag like a keyboard one."""
    app = _app_for(tmp_path)
    _seed_chat(app, "alpha", "beta", "gamma")
    from prompt_toolkit.mouse_events import MouseEventType

    async def _fake_write(text):
        return True

    with patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write):
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_DOWN, 0, 0))
        app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_MOVE, 2, 1))
        rendered = app._render_chat().value
        assert "\x1b[7m" in rendered
        # Every visible row of the drag span ("alpha".."gamma") is
        # reverse-wrapped; the base ANSI is per-character fragments, so each
        # char is wrapped on its own — check per row instead of one
        # contiguous block.
        rows = rendered.split("\n")
        for row in rows[:3]:
            if _strip_ansi(row).strip():
                assert "\x1b[7m" in row, row
        # The drag release yanks (background task) — run it inline in tests.
        with _run_bg_inline(app):
            app._on_chat_mouse(_mouse_event(MouseEventType.MOUSE_UP, 2, 1))
        # Release keeps the selection (drag), so the highlight persists...
        assert "\x1b[7m" in app._render_chat().value
        app.copy_cancel()
        assert "\x1b[7m" not in app._render_chat().value


def test_footer_advertises_word_shortcut_after_mouse_selection(tmp_path) -> None:
    from prompt_toolkit.formatted_text import to_plain_text

    app = _app_for(tmp_path)
    _seed_chat(app, "a", "b")
    assert "2x" not in to_plain_text(app._get_footer_text())
    app._copy_active = True
    app._copy_via_mouse = True
    assert "2x" in to_plain_text(app._get_footer_text())
    # Keyboard entry keeps the plain hint.
    app._copy_via_mouse = False
    assert "2x" not in to_plain_text(app._get_footer_text())


# ── mouse e2e through the real VT100 input layer (PipeInput) ─────────────────
# Xterm SGR packets (enabled by mouse_support=True): "<CBT;X;Y(M|m)" with
# 1-based coordinates. These verify the full chain — parser →
# load_mouse_bindings → Window coordinate conversion → control handler — with
# a real running Application and no mocks in the mouse path. The exact screen
# rows the chat pane occupies depend on the render geometry, so the drive
# routine reads ``chat window.render_info.rowcol_to_yx`` at runtime and
# calibrates the SGR coordinates from it rather than hardcoding a guess.

SGR_DOWN = "\x1b[<0;{x};{y}M"  # left press at screen (col, row), 1-based
SGR_MOVE = "\x1b[<32;{x};{y}M"  # left drag
SGR_UP = "\x1b[<0;{x};{y}m"  # left release
SCROLL_UP = "\x1b[<64;{x};{y}M"  # wheel up (CBT=64)


def _chat_screen_rows(app) -> tuple[int, int]:
    """``(top_screen_row, row_step)`` of the chat pane from the last render.

    ``top_screen_row`` is the 0-based screen row of chat content row 0
    (what the SGR packets must target, since full-screen runs in the
    alternate screen with no rows above the layout); ``row_step`` is the
    screen distance between consecutive content rows.
    """
    ri = app._chat_window.render_info
    assert ri is not None, "chat window not rendered yet"
    mapping = ri._rowcol_to_yx  # noqa: SLF001 - test-only access
    assert (0, 0) in mapping and (1, 0) in mapping, "mapping missing rows"
    y0 = mapping[(0, 0)][0]
    y1 = mapping[(1, 0)][0]
    return (y0, y1 - y0)


@pytest.mark.asyncio
async def test_pipe_input_mouse_drag_selects_and_copies(tmp_path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    _seed_chat(app, "one line of chat", "second line")
    copied = {}

    async def _fake_write(text):
        copied["text"] = text
        return True

    with patch("phoson_cli.fullscreen.app.write_clipboard_text", new=_fake_write):
        with create_pipe_input() as pipe:
            app.app.input = pipe
            app.app.output = DummyOutput()
            observed: dict[str, object] = {}

            async def drive():
                # No asserts in here: an exception would kill the driver.
                # The quit key is in a `finally` so it always goes out —
                # otherwise run_async hangs forever. Observations are
                # recorded for assertions after the app exits.
                try:
                    await asyncio.sleep(0.4)  # wait for the first real render
                    top, row_step = _chat_screen_rows(app)
                    # Press on chat row 0 (col 1), drag to chat row 1 (col 4).
                    # SGR coordinates are 1-based screen (col, row).
                    pipe.send_text(SGR_DOWN.format(x=2, y=top + 1))
                    await asyncio.sleep(0.15)
                    observed["active_after_down"] = app._copy_active
                    pipe.send_text(SGR_MOVE.format(x=5, y=top + row_step + 1))
                    await asyncio.sleep(0.15)
                    pipe.send_text(SGR_UP.format(x=5, y=top + row_step + 1))
                    await asyncio.sleep(0.1)
                    observed["active_after_up"] = app._copy_active  # drag keeps it
                    pipe.send_text("\x1b")  # Esc: leave copy mode
                    await asyncio.sleep(0.1)
                except BaseException as exc:  # noqa: BLE001 - recorded below
                    observed["drive_error"] = exc
                finally:
                    pipe.send_text("\x03")  # quit (always)
                    await asyncio.sleep(0.1)

            asyncio.create_task(drive())
            await asyncio.wait_for(app.app.run_async(), timeout=20)

    assert "drive_error" not in observed, observed.get("drive_error")
    assert observed["active_after_down"] is True
    assert observed["active_after_up"] is True
    # The drag spanned chat row 0 (col 1..end) into row 1 (col 0..4) —
    # "ne line of chat\nseco".
    assert copied.get("text") == "ne line of chat\nseco"
    assert app._copy_active is False


@pytest.mark.asyncio
async def test_pipe_input_mouse_wheel_scroll_still_works(tmp_path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    _seed_chat(app, *([f"row {i}" for i in range(60)]))
    app._auto_scroll = False
    app._chat_scroll_top = 0

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()
        max_scroll = app._total_chat_lines - app._get_visible_window_height()
        assert max_scroll > 3
        observed: dict[str, object] = {}

        async def drive():
            # Same driver pattern as the drag test: record observations,
            # always send the quit key (finally).
            try:
                await asyncio.sleep(0.4)
                top, _ = _chat_screen_rows(app)
                # Wheel up from the very top: must not scroll (clamped) and
                # must not enter copy mode.
                pipe.send_text(SCROLL_UP.format(x=5, y=top + 1))
                await asyncio.sleep(0.15)
                observed["top_after_up"] = app._chat_scroll_top
                observed["active_after_up"] = app._copy_active
                # Wheel down: moves 3 rows and disables auto-scroll.
                pipe.send_text(f"\x1b[<65;5;{top + 1}M")  # CBT=65
                await asyncio.sleep(0.15)
            except BaseException as exc:  # noqa: BLE001 - recorded below
                observed["drive_error"] = exc
            finally:
                pipe.send_text("\x03")  # quit (always)
                await asyncio.sleep(0.1)

        asyncio.create_task(drive())
        await asyncio.wait_for(app.app.run_async(), timeout=20)

    assert "drive_error" not in observed, observed.get("drive_error")
    assert observed["top_after_up"] == 0
    assert observed["active_after_up"] is False
    assert app._chat_scroll_top == 3
    assert app._auto_scroll is False


class _run_bg_inline:
    """Patch ``PhosonApp.app.create_background_task`` to run a coroutine
    inline on a fresh event loop.

    ``create_background_task`` schedules on the running Application loop,
    which does not exist in unit tests (the ``Application`` is never
    ``run_async``). The copy-mode yank fires its clipboard write as a
    background task; in tests we run it to completion inline instead.
    """

    def __init__(self, app) -> None:
        self._app = app

    def __enter__(self):
        self._real = self._app.app.create_background_task

        def _inline(coroutine):
            asyncio.run(coroutine)

        self._app.app.create_background_task = _inline
        return self

    def __exit__(self, *exc) -> None:
        # Restore the real bound method so the throwaway app is left clean.
        self._app.app.create_background_task = self._real
        return None
