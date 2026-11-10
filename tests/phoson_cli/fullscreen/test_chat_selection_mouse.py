"""Drag-to-select in the chat pane, driven by raw SGR mouse reports.

The real ``PhosonApp`` runs on prompt_toolkit's pipe input: every mouse
report below (``ESC [ < code ; x ; y M|m``) goes through the VT100 parser,
the key processor, prompt_toolkit's mouse dispatch and the app's bindings.
Only the clipboard itself is replaced (the text handed to it is recorded).

Screen layout under ``DummyOutput`` (80x40): row 0 is the header, the chat
pane starts at row 1. SGR coordinates are 1-based, so transcript line *n*
(while the pane shows the top) is ``y = n + 2`` and cell *c* is ``x = c + 1``.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.formatted_text import to_formatted_text

from phoson_cli.config import PhosonConfig
from phoson_cli.fullscreen.selection import Selection

LINES = [f"line {i} hello world" for i in range(5)]


def _app_for(tmp_path, lines=LINES, **config_kwargs):
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            **config_kwargs,
        )
        from phoson_cli.fullscreen.app import PhosonApp

        app = PhosonApp(config)
    for line in lines:
        app.sink.blocks.append(Text(line))
    app.sink.dirty = True
    return app


def press(cell: int, line: int) -> str:
    return f"\x1b[<0;{cell + 1};{line + 2}M"


def drag(cell: int, line: int) -> str:
    return f"\x1b[<32;{cell + 1};{line + 2}M"


def release(cell: int, line: int) -> str:
    return f"\x1b[<0;{cell + 1};{line + 2}m"


async def _run(app, steps):
    """Run the app and play *steps*: strings are sent as input, callables run.

    Returns the list of texts handed to the clipboard.
    """
    copied: list[str] = []

    async def fake_write(text, write_raw=None):
        copied.append(text)
        return True

    with (
        create_pipe_input() as pipe,
        patch("phoson_cli.fullscreen.app.write_clipboard_text", new=fake_write),
    ):
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive() -> None:
            await asyncio.sleep(0.1)  # first paint: the pane knows its place
            for step in steps:
                if callable(step):
                    step()
                else:
                    pipe.send_text(step)
                await asyncio.sleep(0.05)
            app.app.exit()

        task = asyncio.create_task(drive())
        await app.app.run_async()
        await task
    return copied


def _notices(app) -> list[str]:
    return [str(block) for block in app.sink.blocks[len(LINES) :]]


def _highlighted(app) -> str:
    fragments = to_formatted_text(app._render_chat())
    return "".join(f[1] for f in fragments if "reverse" in f[0].split())


# ── Drag → copy ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drag_copies_the_range_on_release(tmp_path) -> None:
    app = _app_for(tmp_path)
    copied = await _run(app, [press(5, 0), drag(9, 1), release(9, 1)])
    assert copied == ["0 hello world\nline 1 hel"]
    # The confirmation shows under the composer (footer), not in the chat.
    assert app.footer_flash() == "Copied 24 chars"
    assert "Copied 24 chars" in app._get_footer_text().value
    assert not any("Copied" in notice for notice in _notices(app))


@pytest.mark.asyncio
async def test_reversed_drag_copies_the_same_text(tmp_path) -> None:
    app = _app_for(tmp_path)
    copied = await _run(app, [press(9, 1), drag(5, 0), release(5, 0)])
    assert copied == ["0 hello world\nline 1 hel"]


@pytest.mark.asyncio
async def test_selection_is_highlighted_while_dragging_and_after(tmp_path) -> None:
    app = _app_for(tmp_path)
    seen: dict[str, object] = {}
    cache = app._chat_pane._block_ansi_cache

    def mid_drag() -> None:
        seen["dragging"] = _highlighted(app)
        seen["generation"] = cache.generation
        seen["entries"] = dict(cache._entries)

    def after_release() -> None:
        seen["released"] = _highlighted(app)

    await _run(
        app,
        [press(7, 2), drag(11, 2), mid_drag, release(11, 2), after_release],
    )
    assert seen["dragging"] == "hello"
    assert seen["released"] == "hello"
    # The highlight is an overlay: the per-block render cache was not
    # invalidated, and still holds the very same strings.
    assert cache.generation == seen["generation"]
    assert all(cache._entries[k] is v for k, v in seen["entries"].items())  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_plain_click_copies_nothing_and_selects_nothing(tmp_path) -> None:
    app = _app_for(tmp_path)
    copied = await _run(app, [press(5, 1), release(5, 1)])
    assert copied == []
    assert app._chat_pane.selection is None
    assert _notices(app) == []


@pytest.mark.asyncio
async def test_drag_over_padding_only_copies_nothing(tmp_path) -> None:
    app = _app_for(tmp_path)
    copied = await _run(app, [press(40, 1), drag(60, 1), release(60, 1)])
    assert copied == []
    assert app._chat_pane.selection is None


@pytest.mark.asyncio
async def test_wide_characters_are_copied_by_cell(tmp_path) -> None:
    app = _app_for(tmp_path, lines=["ab日本語cd", "plain"])
    # Cells: a0 b1 日2-3 本4-5 語6-7 c8 d9 — press on 本's second cell.
    copied = await _run(app, [press(5, 0), drag(8, 0), release(8, 0)])
    assert copied == ["本語c"]


@pytest.mark.asyncio
async def test_release_outside_the_pane_still_copies(tmp_path) -> None:
    """The drag ends over the header (row 0): the pane still gets it."""
    app = _app_for(tmp_path)
    copied = await _run(
        app,
        [press(5, 1), drag(3, 0), "\x1b[<32;4;1M", "\x1b[<0;4;1m"],
    )
    assert copied == ["line 0 hello world\nline 1"]
    assert app._chat_pane.selection_pending is False


@pytest.mark.asyncio
async def test_shift_drag_reports_are_left_alone(tmp_path) -> None:
    """A terminal that forwards Shift+drag (code 36) is not an app drag…
    but it never starts one either: the press itself is what arms it."""
    app = _app_for(tmp_path)
    copied = await _run(app, ["\x1b[<36;10;3M", "\x1b[<4;10;3m"])
    assert copied == []
    assert app._chat_pane.selection is None


# ── Clearing ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_click_clears_the_selection(tmp_path) -> None:
    app = _app_for(tmp_path)
    copied = await _run(
        app,
        [press(0, 0), drag(3, 0), release(3, 0), press(10, 3), release(10, 3)],
    )
    assert copied == ["line"]  # the click did not copy again
    assert app._chat_pane.selection is None


@pytest.mark.asyncio
async def test_esc_clears_the_selection_without_arming_the_rewind(tmp_path) -> None:
    app = _app_for(tmp_path)
    seen: dict[str, object] = {}

    def before_esc() -> None:
        seen["selection"] = app._chat_pane.selection

    with patch.object(app, "handle_rewind") as rewind:
        await _run(
            app,
            [press(0, 0), drag(3, 0), release(3, 0), before_esc, app.handle_escape],
        )
    assert seen["selection"] == Selection((0, 0), (0, 3))
    assert app._chat_pane.selection is None
    assert app._last_escape_at == 0.0
    rewind.assert_not_called()


@pytest.mark.asyncio
async def test_esc_mid_run_clears_the_selection_and_still_cancels(tmp_path) -> None:
    app = _app_for(tmp_path)
    cancel = MagicMock(return_value="cancelled")

    def esc_mid_run() -> None:
        with (
            patch.object(app, "_is_run_in_flight", return_value=True),
            patch.object(app, "_cancel_operation", new=cancel),
        ):
            app.handle_escape()

    await _run(app, [press(0, 0), drag(3, 0), release(3, 0), esc_mid_run])
    assert app._chat_pane.selection is None
    cancel.assert_called_once()


@pytest.mark.asyncio
async def test_appended_output_keeps_the_selection_but_relayout_drops_it(
    tmp_path,
) -> None:
    app = _app_for(tmp_path)
    seen: dict[str, object] = {}

    def append_block() -> None:
        app.sink.notify("info", "something new")
        seen["after_append"] = _highlighted(app)

    def relayout() -> None:
        app._chat_pane._block_ansi_cache.clear(0)  # what a theme change does
        app.sink.dirty = True
        seen["after_relayout"] = _highlighted(app)

    await _run(
        app,
        [press(0, 1), drag(3, 1), release(3, 1), append_block, relayout],
    )
    assert seen["after_append"] == "line"
    assert seen["after_relayout"] == ""
    assert app._chat_pane.selection is None


# ── Double / triple click ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_double_click_selects_a_word_and_triple_click_the_line(tmp_path) -> None:
    app = _app_for(tmp_path)
    click = press(8, 2) + release(8, 2)
    copied = await _run(app, [click, click, click])
    assert copied == ["hello", "line 2 hello world"]


# ── Auto-scroll, wheel, switch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dragging_past_the_bottom_edge_scrolls_and_extends(tmp_path) -> None:
    lines = [f"row {i:03d}" for i in range(120)]
    app = _app_for(tmp_path, lines=lines)
    pane = app._chat_pane
    seen: dict[str, int] = {}

    def to_top() -> None:
        pane.scroll_home()

    def note_top() -> None:
        seen["top_before"] = pane._current_top()

    async_wait = "\x1b[<32;3;39M"  # row 38: below the pane, over the composer
    copied = await _run(
        app,
        [to_top, note_top, press(0, 0), drag(2, 3), async_wait, "", "", "", ""],
    )
    assert seen["top_before"] == 0
    assert pane._current_top() > 3  # one step per report, more from the timer
    assert copied == []  # still held: nothing copied yet
    selection = pane.selection
    assert selection is not None
    assert selection.anchor == (0, 0)
    assert selection.cursor[0] > 35  # extended past the first screenful


@pytest.mark.asyncio
async def test_wheel_still_scrolls_the_chat(tmp_path) -> None:
    app = _app_for(tmp_path, lines=[f"row {i}" for i in range(120)])
    pane = app._chat_pane
    seen: dict[str, int] = {}

    def note_bottom() -> None:
        seen["bottom"] = pane._current_top()

    await _run(app, [note_bottom, "\x1b[<64;10;5M"])  # wheel up over the pane
    assert pane._auto_scroll is False
    assert pane._current_top() == seen["bottom"] - 3


@pytest.mark.asyncio
async def test_mouse_selection_can_be_switched_off(tmp_path) -> None:
    app = _app_for(tmp_path, mouse_selection=False)
    copied = await _run(app, [press(5, 0), drag(9, 1), release(9, 1)])
    assert copied == []
    assert app._chat_pane.selection is None
    assert app._chat_pane.selection_pending is False


def test_drag_reports_are_requested_with_mouse_tracking(tmp_path) -> None:
    """DECSET 1002 rides on prompt_toolkit's enable/disable pair."""
    from phoson_cli.fullscreen.app import _report_mouse_drags

    written: list[str] = []
    output = MagicMock()
    output.enable_mouse_support = lambda: written.append("<ptk on>")
    output.disable_mouse_support = lambda: written.append("<ptk off>")
    output.write_raw = written.append
    _report_mouse_drags(output)
    output.enable_mouse_support()
    output.disable_mouse_support()
    assert written == ["<ptk on>", "\x1b[?1002h", "\x1b[?1002l", "<ptk off>"]


def test_config_switch_defaults_on_and_reads_env(monkeypatch) -> None:
    from phoson_cli.config import load_config

    assert PhosonConfig().mouse_selection is True
    monkeypatch.setenv("PHOSON_MOUSE_SELECTION", "0")
    assert load_config().mouse_selection is False


@pytest.mark.asyncio
async def test_release_below_the_text_trims_blank_lines_and_keeps_the_highlight(
    tmp_path,
) -> None:
    app = _app_for(tmp_path)
    seen: dict[str, str] = {}

    def after_notice() -> None:
        seen["highlight"] = _highlighted(app)

    copied = await _run(
        app,
        [press(7, 3), drag(30, 20), release(30, 20), "", after_notice],
    )
    assert copied == ["hello world\nline 4 hello world"]
    # The selection is still shown after the copy was confirmed.
    assert seen["highlight"] == "hello worldline 4 hello world"
