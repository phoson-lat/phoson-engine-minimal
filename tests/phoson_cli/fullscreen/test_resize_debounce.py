"""A width change re-lays a long transcript out once, after it settles."""

import os
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from phoson_cli.config import PhosonConfig
from phoson_cli.fullscreen import chat_pane


def _app_for(tmp_path, lines: int):
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        from phoson_cli.fullscreen.app import PhosonApp

        app = PhosonApp(config)
    for i in range(lines):
        app.sink.blocks.append(Text(f"row {i}"))
    app.sink.dirty = True
    return app


async def _resize_steps(app, monkeypatch, widths, settle: float):
    """Run the app, step the terminal through *widths* (one frame each), then
    wait *settle* seconds. Returns ``(layouts during the steps, layouts after)``
    where a layout is one clear of the per-width block cache."""
    columns = {"value": 100}
    monkeypatch.setattr(
        chat_pane.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((columns["value"], 40)),
    )
    cache = app._chat_pane._block_ansi_cache
    counts: dict[str, int] = {}
    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive() -> None:
            await asyncio.sleep(0.1)
            start = cache.generation
            for width in widths:
                columns["value"] = width
                app.app.invalidate()
                await asyncio.sleep(0.04)  # one frame (min_redraw_interval)
            counts["during"] = cache.generation - start
            await asyncio.sleep(settle)
            counts["after"] = cache.generation - start
            app.app.exit()

        task = asyncio.create_task(drive())
        await app.app.run_async()
        await task
    return counts["during"], counts["after"]


@pytest.mark.asyncio
async def test_window_edge_drag_lays_a_long_transcript_out_once(
    tmp_path, monkeypatch
) -> None:
    app = _app_for(tmp_path, lines=600)
    during, after = await _resize_steps(
        app, monkeypatch, widths=[101, 102, 103, 104, 105], settle=0.3
    )
    assert during == 0  # no re-layout per column while the edge is moving
    assert after == 1  # one, once the width has been stable
    assert app._chat_pane._last_width == 105 - 4


@pytest.mark.asyncio
async def test_short_transcript_follows_the_width_immediately(
    tmp_path, monkeypatch
) -> None:
    app = _app_for(tmp_path, lines=20)
    during, _after = await _resize_steps(
        app, monkeypatch, widths=[101, 102, 103], settle=0.0
    )
    assert during == 3


def test_render_without_a_running_app_is_not_debounced(tmp_path, monkeypatch) -> None:
    """Direct renders (tests, one-off callers) see the new width at once."""
    app = _app_for(tmp_path, lines=600)
    columns = {"value": 100}
    monkeypatch.setattr(
        chat_pane.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((columns["value"], 40)),
    )
    app._render_chat()
    assert app._chat_pane._last_width == 96
    columns["value"] = 120
    app._render_chat()
    assert app._chat_pane._last_width == 116
