"""Tests for host adapters of community-plugin UI blocks (I-110)."""

from io import StringIO
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from phoson_agent import TodoItem, ProgressBlock, TodoListBlock, InteractionResult
from phoson_cli.theme import DARK
from phoson_cli.renderer import Renderer, ClassicSink
from phoson_cli.plugin_ui import (
    SinkPluginUiService,
    NonInteractivePluginUiService,
    render_plugin_block,
)
from phoson_cli.fullscreen.sink import FullScreenSink


def _render(block: object) -> str:
    console = Console(file=StringIO(), width=100, highlight=False)
    with console.capture() as capture:
        console.print(block)
    return capture.get()


def test_todo_and_progress_blocks_render_as_shared_rich_content() -> None:
    todos = TodoListBlock(
        id="todos",
        title="Deployment",
        items=(
            TodoItem(id="build", title="Build", completed=True),
            TodoItem(id="ship", title="Ship", detail="waiting"),
        ),
    )

    output = _render(render_plugin_block(todos, DARK))
    assert "Deployment" in output
    assert "✓ Build" in output
    assert "○ Ship — waiting" in output
    assert "2/3" in _render(
        render_plugin_block(ProgressBlock("progress", "Uploading", 2, 3), DARK)
    )


def test_fullscreen_plugin_block_replace_and_remove_are_in_place() -> None:
    sink = FullScreenSink(lambda: None, DARK)
    ui = SinkPluginUiService(sink, DARK)
    first = ProgressBlock("job", "Starting", 0, 2)
    second = ProgressBlock("job", "Running", 1, 2)

    ui.publish(first)
    ui.replace("job", second)

    assert len(sink.blocks) == 1
    assert "Running 1/2" in _render(sink.blocks[0])
    ui.remove("job")
    assert sink.blocks == []


@pytest.mark.asyncio
async def test_interactive_confirm_delegates_to_existing_confirmation_service() -> None:
    confirmation = type("Confirm", (), {"confirm_bash": AsyncMock(return_value=True)})()
    ui = SinkPluginUiService(FullScreenSink(lambda: None, DARK), DARK, confirmation)

    result = await ui.confirm(title="Deploy", message="Deploy now?", danger="Permanent")

    assert result == InteractionResult(status="submitted")
    confirmation.confirm_bash.assert_awaited_once_with("Deploy\nDeploy now?\nPermanent")


@pytest.mark.asyncio
async def test_non_interactive_ui_never_prompts_and_returns_unavailable(capsys) -> None:
    ui = NonInteractivePluginUiService(DARK)

    result = await ui.confirm(title="Question", message="Continue?")
    ui.publish(ProgressBlock("job", "Finished", 1, 1))

    assert result == InteractionResult(status="unavailable")
    assert "Finished 1/1" in capsys.readouterr().out


def test_classic_sink_forwards_plugin_blocks_to_renderer() -> None:
    renderer = Renderer(console=Console(file=StringIO()), theme=DARK)
    sink = ClassicSink(renderer)
    block = render_plugin_block(ProgressBlock("job", "Classic", 1, 1), DARK)

    sink.publish_plugin_block("job", block)
    assert "Classic 1/1" in renderer.console.file.getvalue()
