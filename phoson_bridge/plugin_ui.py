"""``GuiPluginUiService`` — plugin ``UiBlock``s reach the GUI as JSON.

The engine exposes a ``PluginUiService`` (``phoson_agent.cli_extensions``) and
its host implementation, ``SinkPluginUiService``, **renders the blocks to Rich**
before handing them to the sink: designed for terminal front-ends. The GUI does
not want a terminal rendering but the **neutral** block (the
``NoticeBlock``/``KeyValueBlock``/``TodoListBlock``/``ProgressBlock`` dataclass)
so it can draw it as a native component.

That is why this subclass publishes without rendering: it delegates to the
sink's ``publish_plugin_block``/``replace_plugin_block``/``remove_plugin_block``
hooks (the same duck-typing the engine does), which serialize the block to JSON
and emit it as ``plugin.block`` / ``plugin.block.remove``. The remaining
interactive methods (``ask``/``select``/``form``/``confirm``) are the inherited
ones, which already delegate to ``GuiConfirmation``.

WATCH OUT for ``SessionController._rebuild_engine``: on every rebuild it creates
a new ``SinkPluginUiService`` and injects it into ``engine.context.extra``. That
is why ``install()`` is called whenever the bridge hands out a repl (see
``SessionManager.get``): before any turn, the service is this one again.
"""

from typing import Any, cast

from phoson_cli.plugin_ui import SinkPluginUiService


class GuiPluginUiService(SinkPluginUiService):
    """``SinkPluginUiService`` without the Rich layer: neutral blocks for the GUI."""

    def publish(self, block: Any) -> None:
        publish = getattr(self._sink, "publish_plugin_block", None)
        if publish is not None:
            publish(block.id, block)
            return
        super().publish(block)

    def replace(self, block_id: str, block: Any) -> None:
        replace = getattr(self._sink, "replace_plugin_block", None)
        if replace is not None:
            replace(block_id, block)
            return
        super().replace(block_id, block)

    def remove(self, block_id: str) -> None:
        remove = getattr(self._sink, "remove_plugin_block", None)
        if remove is not None:
            remove(block_id)
            return
        super().remove(block_id)


def install(repl: Any) -> None:
    """Ensure the repl uses ``GuiPluginUiService`` (idempotent and cheap).

    ``_rebuild_engine`` injects a new ``SinkPluginUiService`` (e.g. after a model
    change), which would make blocks arrive as rendered Rich again. It is called
    every time the bridge hands out a repl, so no turn can publish blocks with
    the old service.
    """
    controller = getattr(repl, "_controller", None)
    if controller is None:
        return
    current = getattr(controller, "plugin_ui", None)
    if isinstance(current, GuiPluginUiService):
        return
    # The current service already has the right sink/confirmation/theme: we
    # keep its internals and only swap the service type.
    sink = getattr(current, "_sink", None) or getattr(controller, "sink", None)
    confirmation = getattr(current, "_confirmation", None) or getattr(
        controller, "confirmation", None
    )
    service = GuiPluginUiService(
        cast(Any, sink),
        cast(Any, getattr(current, "theme", None)),
        confirmation=confirmation,
    )
    controller.plugin_ui = service
    engine = getattr(controller, "engine", None)
    context = getattr(engine, "context", None) if engine is not None else None
    if context is not None:
        context.extra["plugin_ui"] = service


__all__ = ["GuiPluginUiService", "install"]
