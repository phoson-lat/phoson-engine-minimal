"""Widgets for the Phoson Textual TUI (Textual migration, phase 3).

Deliberately small: the conversation is a list of rows in a
``VerticalScroll`` — user rows, assistant rows (a
:class:`StreamingTurn` container), tool cards and status lines. All of
them are plain Textual widgets so the sink can update them directly
from the app's event loop (no threads, no ``Rich.Live``).

Child widgets that only exist once streaming starts (reasoning block,
tool cards) are added with ``await self.mount(...)`` — those methods
are async and the sink schedules them inside the app's event loop.
"""

from collections.abc import Iterable

from textual.widget import Widget
from textual.widgets import Static, Markdown, Collapsible
from textual.containers import Vertical


class UserTurn(Static):
    """One user message row."""

    DEFAULT_CSS = """
    UserTurn {
        width: 100%;
        padding: 0 1 0 1;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(f"[bold cyan]you[/]  {text}", markup=True)


class ToolCard(Static):
    """One tool invocation: label, arg summary and (later) result."""

    DEFAULT_CSS = """
    ToolCard {
        width: 100%;
        padding: 0 1 0 3;
    }
    """

    def __init__(self, label: str, detail: str = "") -> None:
        super().__init__(self._markup(label, detail))
        self._label = label

    @staticmethod
    def _markup(
        label: str, detail: str, done: bool = False, error: bool = False
    ) -> str:
        detail_part = f"  {detail}" if detail else ""
        if error:
            return f"[dim]⚙ {label}[/]  [red]✗[/]{detail_part}"
        if done:
            return f"[dim]⚙ {label}[/]  [green]✓[/]{detail_part}"
        return f"[dim]⚙ {label}[/]{detail_part}"

    def set_result(self, summary: str, error: bool = False) -> None:
        self.update(self._markup(self._label, summary, done=not error, error=error))


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
        self._body = Static(text)

    def compose(self) -> Iterable[Widget]:
        yield self._body

    def append_text(self, text: str) -> None:
        self._text += text
        self._body.update(self._text)


class StreamingTurn(Vertical):
    """The in-progress assistant turn: reasoning + content + tool cards.

    The base views (markdown content + status line) are mounted via
    ``compose()`` when the turn itself is mounted, so ``append_token``
    can update them synchronously. Reasoning and tool cards are added
    later while streaming (async mount).

    ``take_reasoning`` pops the accumulated reasoning text so the
    controller can persist it to the node metadata (same contract as
    the classic sink).
    """

    DEFAULT_CSS = """
    StreamingTurn {
        width: 100%;
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
        self._seg_offset = 0  # start index of the current content segment
        self._reasoning = ""
        self._reasoning_view: ReasoningView | None = None
        self._active_md: Markdown | None = None
        self._status_view: Static | None = None
        self._finished = False

    def compose(self) -> Iterable[Widget]:
        # Only the status line exists from the start; content markdown
        # segments and tool cards are added as the run progresses, in
        # chronological order (segment → card → segment → …).
        self._status_view = Static("", classes="turn-status")
        yield self._status_view

    # ── event-driven updates (called by the sink) ─────────────────

    def _insert_anchor(self) -> "Static | Markdown":
        """Mount anchor for new children: above the active segment, or
        above the status line when no segment is open."""
        assert self._status_view is not None
        return self._active_md if self._active_md is not None else self._status_view

    async def append_reasoning(self, text: str) -> None:
        self._reasoning += text
        if self._reasoning_view is None:
            self._reasoning_view = ReasoningView(self._reasoning)
            # Reasoning block goes above the content.
            await self.mount(self._reasoning_view, before=self._insert_anchor())
        else:
            self._reasoning_view.append_text(text)

    async def append_token(self, text: str) -> None:
        self._content += text
        if self._active_md is None:
            self._active_md = Markdown(self._content[self._seg_offset :])
            await self.mount(self._active_md, before=self._status_view)
        else:
            self._active_md.update(self._content[self._seg_offset :])

    def close_segment(self) -> None:
        """Freeze the current content segment (called before a tool card).

        The next token opens a new markdown segment *below* the card, so
        tool cards render above the content that follows them — the same
        order the classic spinner-based renderer produces.
        """
        self._seg_offset = len(self._content)
        self._active_md = None

    @property
    def status_view(self) -> "Static | None":
        return self._status_view

    def set_status(self, text: str) -> None:
        if self._status_view is not None:
            self._status_view.update(text)

    def set_error(self, message: str) -> None:
        if self._status_view is not None:
            self._status_view.update(f"[red]✗ {message}[/]")

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


class AssistantTurn(Static):
    """A finalized assistant message (history replay / non-streamed)."""

    DEFAULT_CSS = """
    AssistantTurn {
        width: 100%;
        padding: 1 0 0 1;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(f"[bold magenta]phos[/]  {text}", markup=True)


class StatusLine(Static):
    """A one-line status notice in the conversation (info/warn/error)."""

    DEFAULT_CSS = """
    StatusLine {
        width: 100%;
        padding: 0 0 0 1;
    }
    """

    def __init__(self, kind: str, message: str) -> None:
        colors = {"info": "dim", "warn": "yellow", "error": "red"}
        icons = {"info": "•", "warn": "⚠", "error": "✗"}
        color = colors.get(kind, "dim")
        icon = icons.get(kind, "•")
        self._kind = kind
        self._message = message
        super().__init__(f"[{color}]{icon} {message}[/]")
