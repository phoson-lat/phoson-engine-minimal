"""Tests for plugin UI blocks (``GuiPluginUiService`` + ``GuiSink``).

Run them with ``uv run python tests/phoson_bridge/harness/plugin_blocks.py``
(``test_harnesses.py`` runs every harness under pytest).

No pytest: asserts and a ``main()`` that prints what it checks. Covers:

1. ``GuiPluginUiService.publish/replace/remove`` emit ``plugin.block`` /
   ``plugin.block.remove`` with the serialized **neutral** block (without the
   Rich layer of the engine's ``SinkPluginUiService``).
2. ``install()`` restores the GUI service on the controller and in
   ``engine.context.extra`` (``_rebuild_engine`` would overwrite it) and is
   idempotent.
3. The legacy path (the engine sends a Rich renderable) degrades to a text
   ``notify`` instead of breaking.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoson_agent import (  # noqa: E402
    TodoItem,
    NoticeBlock,
    ProgressBlock,
    TodoListBlock,
)
from phoson_bridge import plugin_ui  # noqa: E402
from phoson_bridge.sink import GuiSink  # noqa: E402


class _Recording:
    """Fake sink + emit: records the notifications in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, method: str, params: dict) -> None:
        self.events.append((method, params))


class _FakeEngine:
    def __init__(self) -> None:
        self.context = type("Context", (), {"extra": {}})()


class _FakeController:
    def __init__(self, sink: GuiSink) -> None:
        self.sink = sink
        self.confirmation = None
        self.engine = _FakeEngine()
        self.plugin_ui = object()  # what `_rebuild_engine` would leave


class _FakeRepl:
    def __init__(self, controller: _FakeController) -> None:
        self._controller = controller


def main() -> None:
    # 1) Publish/update/remove neutral blocks.
    rec = _Recording()
    sink = GuiSink("s1", rec.emit)
    ui = plugin_ui.GuiPluginUiService(sink, None, confirmation=None)

    notice = NoticeBlock(id="n1", kind="warn", message="cuidado")
    ui.publish(notice)
    method, params = rec.events[-1]
    assert method == "plugin.block", (method, params)
    assert params["sessionId"] == "s1" and params["blockId"] == "n1", params
    assert params["replace"] is False, params
    assert params["block"]["type"] == "NoticeBlock", params
    assert (
        params["block"]["kind"] == "warn" and params["block"]["message"] == "cuidado"
    ), params
    print("ok: publish emite plugin.block con el bloque neutro")

    todo = TodoListBlock(
        id="t1",
        title="Plan",
        items=(
            TodoItem(id="i1", title="uno", completed=True),
            TodoItem(id="i2", title="dos"),
        ),
    )
    ui.replace("t1", todo)
    method, params = rec.events[-1]
    assert method == "plugin.block" and params["replace"] is True, (method, params)
    assert params["block"]["type"] == "TodoListBlock", params
    assert params["block"]["items"][0]["completed"] is True, params
    assert params["block"]["items"][1]["title"] == "dos", params
    print("ok: replace actualiza en sitio (replace=true) con los items serializados")

    ui.publish(
        ProgressBlock(
            id="p1", label="Descarga", completed=3, total=9, detail="archivos"
        )
    )
    params = rec.events[-1][1]
    assert params["block"]["completed"] == 3 and params["block"]["total"] == 9, params
    print("ok: ProgressBlock serializa completed/total/detail")

    ui.remove("t1")
    method, params = rec.events[-1]
    assert method == "plugin.block.remove" and params["blockId"] == "t1", (
        method,
        params,
    )
    print("ok: remove emite plugin.block.remove")

    # 2) install() restores the GUI service and is idempotent.
    controller = _FakeController(sink)
    repl = _FakeRepl(controller)
    plugin_ui.install(repl)
    assert isinstance(controller.plugin_ui, plugin_ui.GuiPluginUiService), (
        controller.plugin_ui
    )
    assert controller.engine.context.extra["plugin_ui"] is controller.plugin_ui
    first = controller.plugin_ui
    plugin_ui.install(repl)  # must not create another service
    assert controller.plugin_ui is first
    print("ok: install() inyecta GuiPluginUiService (y no repite trabajo)")

    # 3) Legacy path: the engine passes a Rich renderable (no dataclass).
    rec.events.clear()

    class _RichRenderable:
        def __str__(self) -> str:
            return "  V tarea renderizada"

    sink.publish_plugin_block("legacy", _RichRenderable())
    method, params = rec.events[-1]
    assert method == "notify" and params["kind"] == "info", (method, params)
    assert "tarea renderizada" in params["message"], params
    print("ok: un renderable Rich degrada a notify de texto (sin romper)")

    print("TODO OK")


if __name__ == "__main__":
    main()
