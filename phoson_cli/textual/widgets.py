"""Widgets for the Phoson Textual TUI.

The conversation is a list of rows in a ``VerticalScroll`` — user rows,
assistant rows (a :class:`StreamingTurn`), tool cards and status lines.
All of them are plain Textual widgets so the sink can update them from
the app's event loop (no threads, no ``Rich.Live``).
"""

from typing import TYPE_CHECKING, cast
from collections.abc import Iterable

from textual.events import Key, Paste
from textual.widget import Widget
from textual.widgets import Static, Markdown, TextArea, Collapsible
from textual.containers import Vertical

if TYPE_CHECKING:
    from .app import PhosonTextualApp


def _escape_markup(text: str) -> str:
    """Neutralize Rich markup so user/tool text cannot restyle the row."""
    return text.replace("[", r"\[")


class UserTurn(Static):
    """One user message row."""

    DEFAULT_CSS = """
    UserTurn {
        width: 100%;
        padding: 0 1 0 1;
    }
    """

    def __init__(self, text: str) -> None:
        preview = text.replace("\n", " ↵ ")
        super().__init__(f"[bold cyan]you[/]  {_escape_markup(preview)}", markup=True)


class ToolCard(Static):
    """One tool invocation: label, arg summary and (later) result."""

    DEFAULT_CSS = """
    ToolCard {
        width: 100%;
        padding: 0 1 0 3;
    }
    """

    def __init__(self, label: str, detail: str = "", tool_call_id: str = "") -> None:
        super().__init__(self._markup(label, detail))
        self._label = label
        self.tool_call_id = tool_call_id

    @staticmethod
    def _markup(
        label: str, detail: str, done: bool = False, error: bool = False
    ) -> str:
        detail_part = f"  {_escape_markup(detail)}" if detail else ""
        safe_label = _escape_markup(label)
        if error:
            return f"[dim]⚙ {safe_label}[/]  [red]✗[/]{detail_part}"
        if done:
            return f"[dim]⚙ {safe_label}[/]  [green]✓[/]{detail_part}"
        return f"[dim]⚙ {safe_label}[/]{detail_part}"

    def set_result(self, summary: str, error: bool = False) -> None:
        self.update(self._markup(self._label, summary, done=not error, error=error))


class SubagentStatusPanel(Static):
    """Live list of parallel sub-agent tasks for one ``agents`` call."""

    DEFAULT_CSS = """
    SubagentStatusPanel {
        width: 100%;
        padding: 0 1 0 3;
        color: $text-muted;
    }
    """

    def __init__(self, tasks: list[str]) -> None:
        self._tasks = list(tasks)
        super().__init__(self._render(running=True))

    def _render(self, *, running: bool, summary: str = "") -> str:
        icon = "◐" if running else "✓"
        lines = ["[dim]subagents[/]"]
        for i, task in enumerate(self._tasks):
            preview = task.replace("\n", " ")[:48]
            lines.append(f"  {icon} {i}  {_escape_markup(preview)}")
        if summary:
            lines.append(f"  [dim]{_escape_markup(summary)}[/]")
        return "\n".join(lines)

    def set_summary(self, summary: str) -> None:
        self.update(self._render(running=False, summary=summary))


class ReasoningView(Collapsible):
    """Collapsible reasoning block for one turn (Ctrl+T toggles it)."""

    DEFAULT_CSS = """
    ReasoningView {
        width: 100%;
        padding: 0 1 0 3;
    }
    ReasoningView > Static {
        padding: 0 1;
    }
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(title="reasoning", collapsed=True)
        self._text = text
        self._body = Static(_escape_markup(text) if text else "")

    def compose(self) -> Iterable[Widget]:
        yield self._body

    def append_text(self, text: str) -> None:
        self._text += text
        self._body.update(_escape_markup(self._text))


class StreamingTurn(Vertical):
    """The in-progress assistant turn: reasoning + content + tool cards.

    The base views (markdown content + status line) are mounted via
    ``compose()`` when the turn itself is mounted, so ``append_token``
    can update them synchronously. Reasoning and tool cards are added
    later while streaming (async mount).
    """

    DEFAULT_CSS = """
    StreamingTurn {
        width: 100%;
        height: auto;
        padding: 1 0 0 1;
    }
    StreamingTurn > Markdown {
        width: 100%;
        padding: 0;
    }
    StreamingTurn .turn-status {
        padding: 0 0 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._content = ""
        self._reasoning = ""
        self._reasoning_view: ReasoningView | None = None
        self._content_view: Markdown | None = None
        self._status_view: Static | None = None
        self._finished = False
        self._cards: dict[str, ToolCard] = {}
        self._last_card: ToolCard | None = None
        self._subagent_panel: SubagentStatusPanel | None = None

    def compose(self) -> Iterable[Widget]:
        self._content_view = Markdown("")
        self._status_view = Static("", classes="turn-status")
        yield self._content_view
        yield self._status_view

    async def append_reasoning(self, text: str) -> None:
        self._reasoning += text
        if self._reasoning_view is None:
            self._reasoning_view = ReasoningView(self._reasoning)
            await self.mount(self._reasoning_view, before=self._content_view)
        else:
            self._reasoning_view.append_text(text)

    def append_token(self, text: str) -> None:
        self._content += text
        if self._content_view is not None:
            self._content_view.update(self._content)

    @property
    def status_view(self) -> Static | None:
        return self._status_view

    def set_status(self, text: str) -> None:
        if self._status_view is not None:
            self._status_view.update(text)

    def set_error(self, message: str) -> None:
        if self._status_view is not None:
            self._status_view.update(f"[red]✗ {_escape_markup(message)}[/]")

    def finalize(self) -> None:
        self._finished = True
        if self._status_view is not None:
            self._status_view.update("")

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def content(self) -> str:
        return self._content

    @property
    def reasoning_view(self) -> ReasoningView | None:
        return self._reasoning_view

    def take_reasoning(self) -> str:
        """Pop the accumulated reasoning text (controller persists it)."""
        text, self._reasoning = self._reasoning, ""
        return text

    def toggle_reasoning(self) -> bool:
        """Toggle the reasoning block. False when the turn has none."""
        if self._reasoning_view is None:
            return False
        self._reasoning_view.collapsed = not self._reasoning_view.collapsed
        return True

    def register_card(self, card: ToolCard) -> None:
        if card.tool_call_id:
            self._cards[card.tool_call_id] = card
        self._last_card = card

    def card_for(self, tool_call_id: str) -> ToolCard | None:
        if tool_call_id and tool_call_id in self._cards:
            return self._cards[tool_call_id]
        return self._last_card

    def register_subagent_panel(self, panel: SubagentStatusPanel) -> None:
        self._subagent_panel = panel

    @property
    def subagent_panel(self) -> SubagentStatusPanel | None:
        return self._subagent_panel


class AssistantTurn(Vertical):
    """A finalized assistant message (history replay)."""

    DEFAULT_CSS = """
    AssistantTurn {
        width: 100%;
        height: auto;
        padding: 1 0 0 1;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> Iterable[Widget]:
        yield Markdown(self._text or "")


class HistoryRule(Static):
    """Divider noting truncated history above the replayed tail."""

    DEFAULT_CSS = """
    HistoryRule {
        width: 100%;
        color: $text-muted;
        padding: 0 0 0 1;
    }
    """

    def __init__(self, above: int) -> None:
        super().__init__(f"[dim]— {above} messages above —[/]")


class StatusLine(Static):
    """A status notice in the conversation (info/warn/error)."""

    DEFAULT_CSS = """
    StatusLine {
        width: 100%;
        padding: 0 0 0 1;
        height: auto;
    }
    """

    def __init__(self, kind: str, message: str) -> None:
        colors = {"info": "dim", "warn": "yellow", "error": "red", "ok": "green"}
        icons = {"info": "•", "warn": "⚠", "error": "✗", "ok": "✓"}
        color = colors.get(kind, "dim")
        icon = icons.get(kind, "•")
        self._kind = kind
        self._message = message
        super().__init__(f"[{color}]{icon} {_escape_markup(message)}[/]")


class Composer(TextArea):
    """Multiline prompt: Enter sends, Shift+Enter inserts a newline.

    Slash-command Tab-complete fills the first matching ``COMMANDS``
    entry when the buffer is a single ``/word``.
    """

    DEFAULT_CSS = """
    Composer {
        height: auto;
        min-height: 3;
        max-height: 12;
        margin: 0 1 1 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            "",
            show_line_numbers=False,
            highlight_cursor_line=False,
            soft_wrap=True,
            tab_behavior="focus",
            **kwargs,
        )

    @property
    def app(self) -> "PhosonTextualApp":
        return cast("PhosonTextualApp", super().app)

    async def _on_key(self, event: Key) -> None:
        self.app._debug_log("composer-key", key=event.key, character=event.character)
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            row, col = self.cursor_location
            lines = self.text.split("\n")
            current = lines[row] if row < len(lines) else ""
            lines[row : row + 1] = [current[:col], current[col:]]
            self.text = "\n".join(lines)
            self.move_cursor((row + 1, 0))  # pyright: ignore[reportUnusedCoroutine]
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.app.submit_composer()
            return
        if event.key == "tab":
            if self._try_slash_complete():
                event.stop()
                event.prevent_default()
                return
        await super()._on_key(event)  # pyright: ignore[reportUnusedCoroutine]

    async def _on_paste(self, event: Paste) -> None:
        self.app._debug_log("paste", text=event.text[:60])
        await super()._on_paste(event)

    def _try_slash_complete(self) -> bool:
        text = self.text
        if not text.startswith("/") or "\n" in text or " " in text:
            return False
        from ..commands import COMMANDS

        word = text.lower()
        matches = [cmd for cmd in sorted(COMMANDS) if cmd.startswith(word)]
        if not matches:
            return False
        completed = matches[0] + " "
        self.text = completed
        self.move_cursor((0, len(completed)))
        return True
