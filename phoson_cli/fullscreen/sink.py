"""AgentEventSink implementation for the full-screen front end.

The classic ``Renderer`` incrementally mutates a persistent terminal via
a Rich ``Live`` display (cursor-relative diffing). There is no
persistent terminal here — every redraw rebuilds the visible transcript
from scratch (see :mod:`.render`) — so this sink instead accumulates an
append-only list of finalized Rich renderables (``blocks``) plus one
mutable in-flight turn (``current_turn``). Every mutation flips a dirty
flag and calls the injected ``on_invalidate`` callback (bound to
``Application.invalidate`` by the caller), mirroring the reference
prototype's "mutate state, then invalidate" streaming pattern.
"""

from dataclasses import dataclass

from phoson_agent import (
    AgentEvent,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_llm.schemas import Message

from ..theme import Theme
from ..formatting import (
    render_notice,
    render_history,
    render_done_line,
    render_user_turn,
    render_error_panel,
    render_tool_done_line,
    render_reasoning_panel,
    render_streaming_panel,
    render_tool_start_line,
    subagent_tasks_from_args,
    render_subagent_start_line,
)
from ..tools.subagent_panel import (
    parse_subagent_metrics,
    render_subagent_summary,
    render_subagent_panel_frame,
)


@dataclass
class CurrentTurn:
    """Mutable state for the in-flight run, rendered fresh every pass."""

    model: str = ""
    message_count: int = 0
    max_steps: int = 0
    current_step: int = 0
    run_cost_usd: float = 0.0
    content: str = ""
    reasoning: str = ""
    show_reasoning: bool = True
    running_tool: bool = False
    subagent_tasks: list[str] | None = None
    subagent_frame: int = 0


class FullScreenSink:
    """``AgentEventSink`` implementation for the full-screen ``PhosonApp``.

    Args:
        on_invalidate: Called after every state mutation (bound to the
            running ``Application.invalidate``).
        theme: The active theme, for building renderables.
    """

    def __init__(self, on_invalidate, theme: Theme, show_reasoning: bool = True) -> None:
        self._on_invalidate = on_invalidate
        self.theme = theme
        self.session_id: str | None = None
        self.dirty = True
        self.show_reasoning_default: bool = show_reasoning

        self.blocks: list[object] = []
        self.current_turn: CurrentTurn | None = None
        self._last_reasoning: str = ""

    def _touch(self) -> None:
        self.dirty = True
        self._on_invalidate()

    def status_text(self) -> str:
        """Short status string for the header bar."""
        turn = self.current_turn
        if turn is None:
            return "Online"
        if turn.subagent_tasks:
            return "Running subagents"
        if turn.running_tool:
            return "Running tool"
        if turn.content or turn.reasoning:
            return "Streaming"
        return f"thinking · step {turn.current_step}/{turn.max_steps}"

    # ── AgentEventSink ───────────────────────────────────────────────────

    def on_user_message(self, text: str, message: Message) -> None:
        self.blocks.append(render_user_turn(text, self.theme))
        self._touch()

    def on_attachments(self, sources: list[str]) -> None:
        if not sources:
            return
        self.blocks.append(
            render_notice("info", f"Attached: {', '.join(sources)}", self.theme)
        )
        self._touch()

    def on_event(self, event: AgentEvent) -> None:
        match event:
            case AgentStartEvent():
                # No meta line here (cli_abel-style: the response starts
                # directly with the assistant label, no model/session line —
                # the header bar already shows the active model).
                self.current_turn = CurrentTurn(
                    model=event.model,
                    message_count=event.message_count,
                    max_steps=event.max_iterations,
                    show_reasoning=self.show_reasoning_default,
                )

            case AgentTokenEvent():
                if self.current_turn is not None:
                    self.current_turn.content += event.content

            case AgentReasoningEvent():
                if self.current_turn is not None:
                    self.current_turn.reasoning += event.content

            case AgentToolStartEvent():
                turn = self.current_turn
                self._freeze_current_text(turn)
                if event.tool_name in {"agent", "agents"}:
                    self.blocks.append(render_subagent_start_line(event, self.theme))
                    tasks = subagent_tasks_from_args(event.tool_name, event.args)
                    if turn is not None and tasks:
                        turn.subagent_tasks = tasks
                        turn.subagent_frame = 0
                else:
                    if turn is not None:
                        turn.running_tool = True
                    self.blocks.append(render_tool_start_line(event, self.theme))

            case AgentToolDoneEvent():
                turn = self.current_turn
                if event.tool_name in {"agent", "agents"}:
                    if turn is not None:
                        turn.subagent_tasks = None
                    metrics = parse_subagent_metrics(event.result)
                    summary = (
                        render_subagent_summary(metrics, theme=self.theme)
                        if metrics
                        else None
                    )
                    if summary is not None:
                        self.blocks.append(summary)
                else:
                    if turn is not None:
                        turn.running_tool = False
                    self.blocks.append(render_tool_done_line(event, self.theme))

            case AgentStepDoneEvent():
                if self.current_turn is not None:
                    self.current_turn.current_step += 1
                    self.current_turn.run_cost_usd += event.step.cost_usd

            case AgentDoneEvent():
                turn = self.current_turn
                if turn is not None:
                    self._last_reasoning = turn.reasoning
                self._freeze_current_text(turn)
                self.current_turn = None
                line = render_done_line(event, self.theme)
                if line is not None:
                    self.blocks.append(line)

            case AgentErrorEvent():
                turn = self.current_turn
                if turn is not None:
                    self._last_reasoning = turn.reasoning
                self.current_turn = None
                self.blocks.append(render_error_panel(event, self.theme))

        self._touch()

    def _freeze_current_text(self, turn: CurrentTurn | None) -> None:
        """Turn whatever's accumulated in ``turn.content`` into a block.

        Called right before a tool card is appended to ``blocks`` (and
        when a turn ends) — without this, all of a turn's tool cards
        would render before *any* of its answer text regardless of when
        they actually happened, since tool cards land in ``blocks``
        immediately while streamed text only accumulates on
        ``current_turn`` until the whole turn finishes. Freezing here
        keeps the transcript in the order things actually happened:
        text so far, then the tool card, then the next segment of text.
        """
        if turn is None or not turn.content:
            return
        self.blocks.append(render_streaming_panel(turn.content, "", False, self.theme))
        turn.content = ""

    def flush_line(self) -> None:
        """Freeze the in-flight turn (cancel/error paths before a terminal event).

        Called by ``SessionController`` right before ``capture_partial_reasoning``
        on cancellation — captures the reasoning here too (rather than
        relying on that follow-up call) since finalizing clears
        ``current_turn``, and there would otherwise be nothing left for
        ``capture_partial_reasoning`` to read.
        """
        turn = self.current_turn
        if turn is None:
            return
        self._last_reasoning = turn.reasoning
        self._freeze_current_text(turn)
        self.current_turn = None
        self._touch()

    def capture_partial_reasoning(self) -> None:
        if self.current_turn is not None:
            self._last_reasoning = self.current_turn.reasoning

    def take_reasoning(self) -> str:
        reasoning, self._last_reasoning = self._last_reasoning, ""
        return reasoning

    def toggle_live_reasoning(self) -> bool:
        """Ctrl+T while streaming: toggle the live thinking block. Returns new state."""
        if self.current_turn is None:
            return True
        self.current_turn.show_reasoning = not self.current_turn.show_reasoning
        self._touch()
        return self.current_turn.show_reasoning

    def expand_reasoning(self, reasoning: str) -> None:
        """Ctrl+T post-turn: append a node's captured reasoning as a block."""
        self.blocks.append(render_reasoning_panel(reasoning, self.theme))
        self._touch()

    def set_session(self, session_id: str) -> None:
        self.session_id = session_id

    def print_history(self, path: list[Message], tail: int) -> None:
        self.blocks.append(render_history(path, self.theme, tail=tail))
        self._touch()

    def notify(self, kind: str, message: str) -> None:
        self.blocks.append(render_notice(kind, message, self.theme))
        self._touch()

    # ── Subagent panel animation tick (driven by the app's spinner task) ──

    def tick_subagent_frame(self) -> bool:
        """Advance the subagent panel animation by one frame.

        Returns True if a subagent panel is active (caller should
        invalidate); does not touch/invalidate itself since it is
        called from a periodic ticker, not a discrete state change.
        """
        turn = self.current_turn
        if turn is None or not turn.subagent_tasks:
            return False
        turn.subagent_frame += 1
        return True

    def render_subagent_panel(self):
        """Current subagent panel renderable, or None when inactive."""
        turn = self.current_turn
        if turn is None or not turn.subagent_tasks:
            return None
        return render_subagent_panel_frame(
            turn.subagent_tasks, turn.subagent_frame, self.theme
        )


__all__ = ["FullScreenSink", "CurrentTurn"]
