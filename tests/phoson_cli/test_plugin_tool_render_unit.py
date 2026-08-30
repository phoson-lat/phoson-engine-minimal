"""Tests for controller-scoped plugin tool presentation (I-110)."""

from io import StringIO

import pytest
from rich.console import Console

from phoson_agent import Plugin, ToolRenderSpec
from phoson_cli.theme import DARK
from phoson_agent.models import (
    AgentToolDoneEvent,
    AgentToolStartEvent,
    AgentToolComposingEvent,
)
from phoson_cli.renderer import Renderer
from phoson_cli.formatting import (
    render_tool_done_line,
    render_tool_start_line,
    build_tool_render_registry,
)
from phoson_cli.fullscreen.sink import FullScreenSink


class _Plugin(Plugin):
    @property
    def name(self) -> str:
        return "example"

    def get_tool_render_specs(self) -> list[ToolRenderSpec]:
        return [
            ToolRenderSpec(tool_name="example_tool", verb="checking example", icon="◌")
        ]


def _render(renderable: object) -> str:
    console = Console(file=StringIO(), width=100, highlight=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_plugin_tool_card_uses_declared_icon_and_verb() -> None:
    registry = build_tool_render_registry([_Plugin()], ["example_tool"])
    start = AgentToolStartEvent(tool_name="example_tool", args={"name": "one"})
    done = AgentToolDoneEvent(tool_name="example_tool", result="ok", duration_ms=1)

    assert "◌ checking example" in _render(
        render_tool_start_line(start, DARK, registry)
    )
    assert "◌ checking example" in _render(
        render_tool_done_line(done, DARK, registry=registry)
    )


def test_renderer_and_fullscreen_sink_use_their_own_registry() -> None:
    registry = build_tool_render_registry([_Plugin()], ["example_tool"])
    event = AgentToolComposingEvent(tool_name="example_tool")

    renderer = Renderer(console=Console(file=StringIO()), theme=DARK)
    renderer.set_tool_render_registry(registry)
    renderer._on_tool_composing(event)
    assert "checking example" in renderer._spinner._label

    sink = FullScreenSink(lambda: None, DARK)
    sink.set_tool_render_registry(registry)
    sink.begin_activity()
    sink.on_event(event)
    assert sink.activity_text() == "◌ checking example…"


def test_tool_render_registries_do_not_leak_between_sessions() -> None:
    with_plugin = build_tool_render_registry([_Plugin()], ["example_tool"])
    without_plugin = build_tool_render_registry([], ["example_tool"])

    assert "checking example" in _render(
        render_tool_start_line(
            AgentToolStartEvent(tool_name="example_tool"), DARK, with_plugin
        )
    )
    assert "example tool" in _render(
        render_tool_start_line(
            AgentToolStartEvent(tool_name="example_tool"), DARK, without_plugin
        )
    )


@pytest.mark.parametrize(
    "plugin, tools, message",
    [
        (_Plugin(), [], "unknown tool"),
        (
            type(
                "BuiltinOverride",
                (Plugin,),
                {
                    "name": property(lambda self: "override"),
                    "get_tool_render_specs": lambda self: [
                        ToolRenderSpec("bash", "overriding", "!")
                    ],
                },
            )(),
            ["bash"],
            "cannot override built-in",
        ),
    ],
)
def test_tool_render_registry_rejects_invalid_specs(plugin, tools, message) -> None:
    with pytest.raises(ValueError, match=message):
        build_tool_render_registry([plugin], tools)
