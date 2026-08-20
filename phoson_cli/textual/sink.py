"""Textual implementation of :class:`AgentEventSink` (phase 3).

The sink is called from inside the app's event loop (the controller
runs as a task the app owns), so it updates the conversation widgets
directly. No Rich console, no ``Live``, no threads — Textual owns all
rendering.
"""

from typing import TYPE_CHECKING
from collections.abc import Sequence

from phoson_agent import (
    AgentDoneEvent,
    AgentErrorEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)

from .widgets import ToolCard, UserTurn, StatusLine, AssistantTurn, StreamingTurn
from ..renderer import _tool_label, _args_preview

if TYPE_CHECKING:
    from .app import PhosonTextualApp


class TextualSink:
    """Routes controller events into the TUI conversation widgets."""

    def __init__(self, app: "PhosonTextualApp") -> None:
        self._app = app
        self._last_card: ToolCard | None = None

    # ── AgentEventSink ────────────────────────────────────────────

    def on_user_message(self, text: str, message: "object") -> None:
        # The user row goes above the (already created) assistant turn.
        turn = self._app.current_turn()
        self._app.schedule(self._app.conversation().mount(UserTurn(text), before=turn))
        self._app.scroll_conversation()

    def on_attachments(self, sources: "list[str]") -> None:
        if sources:
            names = ", ".join(s.replace("\n", " ")[:60] for s in sources[:8])
            self._app.schedule(
                self._app.conversation().mount(StatusLine("info", f"attached: {names}"))
            )

    def on_event(self, event: "object") -> None:
        match event:
            case AgentTokenEvent():
                self._current_turn().append_token(event.content)
                self._app.follow_if_pinned()
            case AgentReasoningEvent():
                self._app.schedule(self._current_turn().append_reasoning(event.content))
                self._app.follow_if_pinned()
            case AgentToolStartEvent():
                self._on_tool_start(event)
            case AgentToolDoneEvent():
                self._on_tool_done(event)
            case AgentStepDoneEvent():
                self._on_step_done(event)
            case AgentDoneEvent():
                self._on_done(event)
            case AgentErrorEvent():
                self._on_error(event)

    def flush_line(self) -> None:
        # No spinner in the TUI: nothing to flush.
        return

    def capture_partial_reasoning(self) -> None:
        # Reasoning already lives in the current turn widget; nothing
        # to copy (the classic renderer copies its live buffer).
        return

    def take_reasoning(self) -> str:
        turn = self._app.current_turn()
        if turn is None:
            return ""
        return turn.take_reasoning()

    def set_session(self, session_id: str) -> None:
        self._app.update_status_bar()

    def print_history(self, path: Sequence["object"], tail: int) -> None:
        for node in path[: max(0, tail)]:
            text = str(getattr(node, "content", "") or "")
            role = getattr(node, "role", "")
            if role == "user":
                self._app.schedule(self._app.conversation().mount(UserTurn(text)))
            elif role == "assistant" and text:
                self._app.schedule(self._app.conversation().mount(AssistantTurn(text)))
        if path:
            self._app.scroll_conversation()

    def notify(self, kind: str, message: str) -> None:
        self._app.schedule(self._app.conversation().mount(StatusLine(kind, message)))
        self._app.scroll_conversation()

    # ── internals ─────────────────────────────────────────────────

    def _current_turn(self) -> StreamingTurn:
        turn = self._app.current_turn()
        assert turn is not None, "on_event received outside a run"
        return turn

    def _on_tool_start(self, event: AgentToolStartEvent) -> None:
        label = _tool_label(event)
        detail = _args_preview(event.tool_name, event.args or {})
        card = ToolCard(label, detail)
        self._last_card = card
        turn = self._current_turn()
        self._app.schedule(turn.mount(card, before=turn.status_view))
        self._app.follow_if_pinned()

    def _on_tool_done(self, event: AgentToolDoneEvent) -> None:
        if self._last_card is not None:
            if event.error:
                err_short = event.error.splitlines()[0][:80]
                self._last_card.set_result(
                    f"{event.duration_ms}ms · {err_short}", error=True
                )
            else:
                self._last_card.set_result(f"{event.duration_ms}ms")
            self._last_card = None
        self._app.follow_if_pinned()

    def _on_step_done(self, event: AgentStepDoneEvent) -> None:
        self._app.update_status_bar()

    def _on_done(self, event: "object") -> None:
        turn = self._app.current_turn()
        if turn is not None and not turn.finished:
            turn.finalize()
        self._app.follow_if_pinned()

    def _on_error(self, event: AgentErrorEvent) -> None:
        turn = self._app.current_turn()
        if turn is not None and not turn.finished:
            turn.set_error(event.message)
        self._app.follow_if_pinned()
