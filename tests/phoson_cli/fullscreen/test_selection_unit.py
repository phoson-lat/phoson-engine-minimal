"""Unit tests: selection → text / highlight, and the clipboard write side."""

import base64
from unittest.mock import patch

import pytest
from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from phoson_cli.fullscreen import clipboard
from phoson_cli.hyperlinks import osc8_passthrough
from phoson_cli.fullscreen.selection import (
    Selection,
    plain_line,
    word_cells,
    slice_cells,
    selected_text,
    highlight_window,
)

# ── Text extraction ───────────────────────────────────────────────────────────


def _lines(*lines: str):
    return lambda n: lines[n]


def test_plain_line_strips_sgr_and_newline() -> None:
    assert plain_line("\x1b[1;31mred\x1b[0m and \x1b[7mrev\x1b[0m\n") == "red and rev"


def test_plain_line_drops_osc8_links_and_restores_the_anchor_space() -> None:
    raw = "see \x1b]8;;https://example.com\x1b\\the docs\x1b]8;;\x1b\\   \n"
    wrapped = osc8_passthrough(raw)
    assert " " in wrapped  # the close was anchored on a figure space
    text = plain_line(wrapped)
    assert text == "see the docs   "
    assert "example.com" not in text


def test_single_line_selection_includes_both_end_cells() -> None:
    text = selected_text(_lines("hello world"), Selection((0, 6), (0, 10)))
    assert text == "world"


def test_multi_line_selection_joins_with_newlines_and_trims_padding() -> None:
    lines = _lines(
        "\x1b[32mfirst line\x1b[0m      \n",
        "  middle   \n",
        "last line here\n",
    )
    text = selected_text(lines, Selection((0, 6), (2, 3)))
    assert text == "line\n  middle\nlast"


def test_reversed_drag_copies_the_same_text() -> None:
    lines = _lines("alpha beta\n", "gamma delta\n")
    forward = selected_text(lines, Selection((0, 6), (1, 4)))
    backward = selected_text(lines, Selection((1, 4), (0, 6)))
    assert forward == backward == "beta\ngamma"


def test_wide_characters_are_selected_by_cell() -> None:
    line = "ab日本語cd"  # cells: a0 b1 日2-3 本4-5 語6-7 c8 d9
    assert slice_cells(line, 2, 4) == "日"
    assert slice_cells(line, 3, 5) == "日本"  # either cell takes the whole char
    assert slice_cells(line, 8, None) == "cd"
    text = selected_text(_lines(line), Selection((0, 5), (0, 8)))
    assert text == "本語c"


def test_emoji_is_two_cells_wide() -> None:
    line = "ok 🎉 done"  # 🎉 occupies cells 3-4
    assert slice_cells(line, 4, 5) == "🎉"
    assert slice_cells(line, 5, None) == " done"


def test_selection_past_the_end_of_a_line_is_empty_there() -> None:
    text = selected_text(_lines("ab\n", "cdef\n"), Selection((0, 10), (1, 1)))
    assert text == "\ncd"


def test_word_cells() -> None:
    line = "run src/app.py now"
    assert word_cells(line, 6) == (4, 13)
    assert word_cells(line, 3) is None  # on a space
    assert word_cells(line, 40) is None  # past the end
    assert word_cells("ab 日本 cd", 5) == (3, 6)


# ── Highlight overlay ─────────────────────────────────────────────────────────


def _reversed_text(fragments) -> str:
    return "".join(f[1] for f in fragments if "reverse" in f[0].split())


def test_highlight_marks_only_the_selected_cells() -> None:
    window = to_formatted_text(ANSI("\x1b[31mhello world\x1b[0m\nsecond line\nthird"))
    out = highlight_window(window, 10, Selection((10, 6), (11, 5)))
    assert _reversed_text(out) == "worldsecond"
    assert "".join(f[1] for f in out) == "hello world\nsecond line\nthird"
    # The original colour is kept under the highlight.
    assert any(f[0].split() == ["ansired", "reverse"] for f in out)


def test_highlight_skips_trailing_padding_and_lines_outside_the_window() -> None:
    window = to_formatted_text(ANSI("text     \nmore"))
    # Starts above the window (line 3) and ends on its second row.
    out = highlight_window(window, 5, Selection((3, 2), (6, 1)))
    assert _reversed_text(out) == "textmo"


def test_highlight_flips_cells_that_are_already_reversed() -> None:
    window = to_formatted_text(ANSI("\x1b[7mrev\x1b[0m"))
    out = highlight_window(window, 0, Selection((0, 0), (0, 2)))
    assert all("noreverse" in f[0] for f in out if f[1].strip())


def test_highlight_keeps_zero_width_escapes_in_place() -> None:
    raw = osc8_passthrough("\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\ tail")
    window = to_formatted_text(ANSI(raw))
    out = highlight_window(window, 0, Selection((0, 0), (0, 3)))
    assert _reversed_text(out) == "link"
    escapes = [f[1] for f in out if "[ZeroWidthEscape]" in f[0]]
    assert escapes == [f[1] for f in window if "[ZeroWidthEscape]" in f[0]]


# ── Clipboard write ───────────────────────────────────────────────────────────


def _decode_osc52(sequence: str) -> str:
    assert sequence.startswith("\x1b]52;c;") and sequence.endswith("\x07")
    return base64.b64decode(sequence[len("\x1b]52;c;") : -1]).decode("utf-8")


def test_osc52_sequence_round_trips_unicode(monkeypatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    sequence = clipboard.osc52_sequence("héllo 日本\nline 2")
    assert sequence is not None
    assert _decode_osc52(sequence) == "héllo 日本\nline 2"


def test_osc52_sequence_is_capped(monkeypatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    assert clipboard.osc52_sequence("x" * 74_000) is not None
    assert clipboard.osc52_sequence("x" * 80_000) is None


def test_osc52_sequence_adds_tmux_passthrough(monkeypatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    sequence = clipboard.osc52_sequence("hi")
    assert sequence is not None
    plain = "\x1b]52;c;aGk=\x07"
    assert sequence == plain + "\x1bPtmux;\x1b\x1b]52;c;aGk=\x07\x1b\\"


@pytest.mark.parametrize(
    ("platform", "env", "tools", "expected"),
    [
        ("darwin", {}, {"pbcopy"}, ["pbcopy"]),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, {"wl-copy", "xclip"}, ["wl-copy"]),
        (
            "linux",
            {"DISPLAY": ":0"},
            {"xclip", "xsel"},
            ["xclip", "-selection", "clipboard", "-in"],
        ),
        ("linux", {"DISPLAY": ":0"}, {"xsel"}, ["xsel", "--clipboard", "--input"]),
        ("linux", {}, {"clip.exe"}, ["clip.exe"]),  # WSL
        ("win32", {}, {"clip.exe"}, ["clip.exe"]),
        ("linux", {}, {"xclip"}, None),  # no display to talk to
    ],
)
def test_copy_command_per_platform(monkeypatch, platform, env, tools, expected) -> None:
    for name in ("WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(clipboard.sys, "platform", platform)
    monkeypatch.setattr(
        clipboard.shutil, "which", lambda tool: tool if tool in tools else None
    )
    assert clipboard._copy_command() == expected


@pytest.mark.asyncio
async def test_write_prefers_the_platform_tool(monkeypatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    ran: list[tuple[list[str], str]] = []
    written: list[str] = []

    async def fake_run(command, text):
        ran.append((command, text))
        return True

    with (
        patch.object(clipboard, "_copy_command", return_value=["pbcopy"]),
        patch.object(clipboard, "_run_copy_command", new=fake_run),
    ):
        assert await clipboard.write_clipboard_text("hello", written.append) is True
    assert ran == [(["pbcopy"], "hello")]
    assert written == []  # the tool took it: no OSC 52 on top


@pytest.mark.asyncio
async def test_write_uses_osc52_over_ssh_even_with_a_tool(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    monkeypatch.delenv("TMUX", raising=False)
    written: list[str] = []
    with patch.object(clipboard, "_copy_command", return_value=["pbcopy"]) as pick:
        assert await clipboard.write_clipboard_text("remote", written.append) is True
    pick.assert_not_called()
    assert [_decode_osc52(s) for s in written] == ["remote"]


@pytest.mark.asyncio
async def test_write_falls_back_to_osc52_when_the_tool_fails(monkeypatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    written: list[str] = []

    async def failing(command, text):
        return False

    with (
        patch.object(clipboard, "_copy_command", return_value=["xclip"]),
        patch.object(clipboard, "_run_copy_command", new=failing),
    ):
        assert await clipboard.write_clipboard_text("text", written.append) is True
    assert [_decode_osc52(s) for s in written] == ["text"]


@pytest.mark.asyncio
async def test_write_reports_failure_with_no_tool_and_an_oversized_payload(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    written: list[str] = []
    with patch.object(clipboard, "_copy_command", return_value=None):
        ok = await clipboard.write_clipboard_text("x" * 200_000, written.append)
    assert ok is False
    assert written == []


@pytest.mark.asyncio
async def test_run_copy_command_feeds_stdin_to_a_real_process(tmp_path) -> None:
    """The subprocess path itself, with ``tee`` standing in for the tool."""
    target = tmp_path / "clip.txt"
    assert (
        await clipboard._run_copy_command(["tee", str(target)], "héllo\n日本") is True
    )
    assert target.read_text(encoding="utf-8") == "héllo\n日本"
    assert await clipboard._run_copy_command(["/nonexistent/copy-tool"], "x") is False
