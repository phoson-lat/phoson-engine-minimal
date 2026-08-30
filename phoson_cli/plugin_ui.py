"""Host adapters for the neutral community-plugin UI contracts (I-110)."""

from dataclasses import dataclass

from rich.text import Text
from rich.table import Table
from rich.console import Group

from phoson_agent import (
    Choice,
    UiBlock,
    FormField,
    NoticeBlock,
    KeyValueBlock,
    ProgressBlock,
    TodoListBlock,
    PluginUiService,
    InteractionResult,
)

from .theme import Theme
from .formatting import render_notice
from .ui_protocols import AgentEventSink


def render_plugin_block(block: UiBlock, theme: Theme) -> object:
    """Adapt a neutral block to a Rich renderable shared by both frontends."""
    if isinstance(block, NoticeBlock):
        return render_notice(block.kind, block.message, theme)
    if isinstance(block, KeyValueBlock):
        table = Table(
            title=block.title,
            show_header=False,
            border_style=theme.muted_deep,
        )
        table.add_column(style=f"bold {theme.accent}")
        table.add_column(style=theme.text)
        for key, value in block.items:
            table.add_row(key, value)
        return table
    if isinstance(block, TodoListBlock):
        lines = [Text(block.title, style=f"bold {theme.accent}")]
        for item in block.items:
            marker = "✓" if item.completed else "○"
            line = Text(
                f"  {marker} {item.title}",
                style=theme.ok if item.completed else theme.text,
            )
            if item.detail:
                line.append(f" — {item.detail}", style=theme.muted)
            lines.append(line)
        return Group(*lines)
    if isinstance(block, ProgressBlock):
        detail = f" — {block.detail}" if block.detail else ""
        progress = (
            f" {block.completed}/{block.total}"
            if block.completed is not None and block.total is not None
            else ""
        )
        return Text(f"  ◌ {block.label}{progress}{detail}", style=theme.muted)
    raise TypeError(f"Unsupported plugin UI block: {type(block).__name__}")


@dataclass
class NonInteractivePluginUiService(PluginUiService):
    """Safe one-shot/CI adapter: output blocks, never request stdin."""

    theme: Theme

    def publish(self, block: UiBlock) -> None:
        print(render_plugin_block(block, self.theme))

    def replace(self, block_id: str, block: UiBlock) -> None:
        self.publish(block)

    def remove(self, block_id: str) -> None:  # noqa: ARG002
        pass

    def set_status(self, key: str, label: str | None) -> None:  # noqa: ARG002
        pass

    async def confirm(
        self, *, title: str, message: str, danger: str | None = None
    ) -> InteractionResult:
        return InteractionResult(status="unavailable")

    async def select(
        self, *, title: str, message: str, choices: list[Choice]
    ) -> InteractionResult:
        return InteractionResult(status="unavailable")

    async def form(self, *, title: str, fields: list[FormField]) -> InteractionResult:
        return InteractionResult(status="unavailable")


class SinkPluginUiService(PluginUiService):
    """Block publisher shared by interactive sinks; prompts are host-specific."""

    def __init__(
        self,
        sink: AgentEventSink,
        theme: Theme,
        confirmation: object | None = None,
    ) -> None:
        self._sink = sink
        self._theme = theme
        self._confirmation = confirmation

    def _id(self, block_id: str) -> str:
        return block_id

    def publish(self, block: UiBlock) -> None:
        publish = getattr(self._sink, "publish_plugin_block", None)
        if publish is not None:
            publish(self._id(block.id), render_plugin_block(block, self._theme))
        else:
            self._sink.notify("info", str(render_plugin_block(block, self._theme)))

    def replace(self, block_id: str, block: UiBlock) -> None:
        replace = getattr(self._sink, "replace_plugin_block", None)
        if replace is not None:
            replace(self._id(block_id), render_plugin_block(block, self._theme))
        else:
            self.publish(block)

    def remove(self, block_id: str) -> None:
        remove = getattr(self._sink, "remove_plugin_block", None)
        if remove is not None:
            remove(self._id(block_id))

    def set_status(self, key: str, label: str | None) -> None:
        self._sink.notify("info", f"{key}: {label}" if label else f"{key}: cleared")

    async def confirm(
        self, *, title: str, message: str, danger: str | None = None
    ) -> InteractionResult:
        confirm_bash = getattr(self._confirmation, "confirm_bash", None)
        if confirm_bash is None:
            return InteractionResult(status="unavailable")
        prompt = "\n".join(part for part in (title, message, danger) if part)
        return InteractionResult(
            status="submitted" if await confirm_bash(prompt) else "cancelled"
        )

    async def select(
        self, *, title: str, message: str, choices: list[Choice]
    ) -> InteractionResult:
        # Selection/forms require a generic host picker. Returning an explicit
        # result is safe for scripts and older frontends; they are expanded in
        # a follow-up without changing this stable API.
        return InteractionResult(status="unavailable")

    async def form(self, *, title: str, fields: list[FormField]) -> InteractionResult:
        return InteractionResult(status="unavailable")
