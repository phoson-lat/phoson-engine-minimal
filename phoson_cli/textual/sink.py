"""Textual implementation of :class:`AgentEventSink` (phase 3).

The sink is called from inside the app's event loop (the controller
runs as a task the app owns), so it updates the conversation widgets
directly. No Rich console, no ``Live``, no threads — Textual owns all
rendering.
"""

import asyncio
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
        # Turn mutations (token/reasoning/tool updates) are serialized
        # through a FIFO queue so async mounts never interleave.
        self._turn_queue: asyncio.Queue = asyncio.Queue()
        self._turn_worker: asyncio.Task | None = None

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
                self._enqueue_turn(self._current_turn().append_token(event.content))
            case AgentReasoningEvent():
                self._enqueue_turn(self._current_turn().append_reasoning(event.content))
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

    def _enqueue_turn(self, coro) -> None:
        self._turn_queue.put_nowait(coro)
        if self._turn_worker is None or self._turn_worker.done():
            self._turn_worker = asyncio.ensure_future(self._drain_turn_queue())
            self._turn_worker.add_done_callback(self._app._log_task_error)

    async def _drain_turn_queue(self) -> None:
        while True:
            coro = await self._turn_queue.get()
            try:
                await coro
            except Exception as exc:  # noqa: BLE001 — keep the run alive
                self._app.log.warning("turn update error: %s", exc)
            if self._turn_queue.empty():
                self._turn_worker = None
                return

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

        async def _place_card() -> None:
            # Freeze the current content segment so the card sits above
            # the text that streams after the tool.
            turn.close_segment()
            await turn.mount(card, before=turn.status_view)

        self._enqueue_turn(_place_card())

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

    def _on_step_done(self, event: AgentStepDoneEvent) -> None:
        self._app.update_status_bar()

    def _on_done(self, event: "object") -> None:
        turn = self._app.current_turn()
        if turn is not None and not turn.finished:
            turn.finalize()

    def _on_error(self, event: AgentErrorEvent) -> None:
        turn = self._app.current_turn()
        if turn is not None and not turn.finished:
            turn.set_error(event.message)
