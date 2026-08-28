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

import time
import asyncio
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
from ..animations import SPINNER_FRAMES
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
    # Live metrics for the sub-agent panel (E2): the per-call tracker the
    # sub-agent tool created and pushed via ``on_subagent_progress``. The
    # panel renders from it when present; without it the table falls back
    # to the static "waiting" cells.
    subagent_progress: object | None = None
    activity_frame: int = 0
    thinking_phrase_index: int = 0


class FullScreenSink:
    """``AgentEventSink`` implementation for the full-screen ``PhosonApp``.

    Args:
        on_invalidate: Called after every state mutation (bound to the
            running ``Application.invalidate``).
        theme: The active theme, for building renderables.
    """

    def __init__(
        self, on_invalidate, theme: Theme, show_reasoning: bool = True
    ) -> None:
        self._on_invalidate = on_invalidate
        self.theme = theme
        self.session_id: str | None = None
        self.dirty = True
        self.show_reasoning_default: bool = show_reasoning

        self.blocks: list[object] = []
        self.current_turn: CurrentTurn | None = None
        self._last_reasoning: str = ""
        # Args + transcript block of in-flight regular tool calls, keyed by
        # tool_call_id (C1). Done events don't carry args, and the done card
        # REPLACES the start line so each call renders as exactly one card
        # (appending would duplicate the header).
        self._pending_tool_calls: dict[str, tuple[dict, object]] = {}
        # Streaming repaint throttle state (see touch_streaming).
        self._last_stream_repaint: float = 0.0
        self._stream_repaint_pending: asyncio.TimerHandle | None = None

    def _touch(self) -> None:
        self.dirty = True
        self._on_invalidate()

    def touch_streaming(self) -> None:
        """Dirty-flag update throttled during live token streaming.

        Tokens can arrive hundreds of times per second; with the ANSI
        block cache each frame is cheap, but waking prompt_toolkit's
        renderer that often is still wasted work. Coalesce repaints to
        ~16fps (REPAINT_INTERVAL_SECONDS) while a turn is in flight.
        The final token always schedules a trailing repaint so the last
        chunk of text is never left unrendered.
        """
        self.dirty = True
        now = time.monotonic()
        if self.current_turn is None:
            self._touch()
            return
        if now - self._last_stream_repaint >= REPAINT_INTERVAL_SECONDS:
            self._last_stream_repaint = now
            self._touch()
            return
        if (
            self._stream_repaint_pending is None
            or self._stream_repaint_pending.cancelled()
        ):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (unit tests, sync callers): repaint now.
                self._last_stream_repaint = now
                self._touch()
                return
            delay = REPAINT_INTERVAL_SECONDS - (now - self._last_stream_repaint)
            self._stream_repaint_pending = loop.call_later(max(delay, 0.0), self._touch)

    def cancel_stream_throttle(self) -> None:
        """Drop any pending throttled repaint timer (turn finished)."""
        if self._stream_repaint_pending is not None:
            self._stream_repaint_pending.cancel()
            self._stream_repaint_pending = None

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

    # ── Transient in-chat activity indicator ──────────────────────────────

    def begin_activity(self) -> None:
        """Show the in-chat spinner immediately after the user sends a turn.

        The provider may take a noticeable time to emit ``AgentStartEvent``.
        Creating the live turn here covers that otherwise silent interval;
        the real start event then replaces it with the model/step metadata.
        """
        if self.current_turn is None:
            self.current_turn = CurrentTurn(show_reasoning=self.show_reasoning_default)
            self._touch()

    def end_pending_activity(self) -> None:
        """Remove an unclaimed pre-provider placeholder, if any.

        Normal agent completion/error events already clear ``current_turn``.
        This covers an abnormal provider return/cancellation before it emitted
        even ``AgentStartEvent`` so a ``Thinking…`` spinner cannot get stuck.
        """
        turn = self.current_turn
        if turn is not None and not (
            turn.model
            or turn.content
            or turn.reasoning
            or turn.running_tool
            or turn.subagent_tasks
        ):
            self.current_turn = None
            self._touch()

    def activity_text(self) -> str:
        """Human-readable phase for the transient chat activity line.

        The *thinking* phase rotates through ``_THINKING_PHRASES`` (one every
        ``_THINKING_PHRASE_TICKS`` ticks) so a long wait reads as progress
        rather than a frozen label. The other phases are informational and
        stay fixed: they describe the real state, not a mood.
        """
        turn = self.current_turn
        if turn is None:
            return ""
        if turn.subagent_tasks:
            return "Running subagents…"
        if turn.running_tool:
            return "Running tool…"
        if turn.content:
            return "Streaming…"
        return _THINKING_PHRASES[turn.thinking_phrase_index % len(_THINKING_PHRASES)]

    def activity_frame(self) -> str:
        """Current spinner glyph for the active turn (empty when idle)."""
        turn = self.current_turn
        if turn is None:
            return ""
        index = turn.activity_frame % len(SPINNER_FRAMES)
        return SPINNER_FRAMES[index]

    def tick_activity_frame(self) -> bool:
        """Advance the in-chat spinner; return whether a turn is active.

        Also rotates the thinking phrase once per ``_THINKING_PHRASE_TICKS``
        ticks (~2.5 s) — but only while actually thinking, so the phase
        label doesn't churn under "Streaming…" / "Running tool…".
        """
        turn = self.current_turn
        if turn is None:
            return False
        turn.activity_frame += 1
        if (
            not turn.content
            and not turn.running_tool
            and not turn.subagent_tasks
            and turn.activity_frame % _THINKING_PHRASE_TICKS == 0
        ):
            turn.thinking_phrase_index += 1
        return True

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
                    self.touch_streaming()

            case AgentReasoningEvent():
                if self.current_turn is not None:
                    self.current_turn.reasoning += event.content
                    self.touch_streaming()

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
                    # The start line is live feedback. Keep its object so the
                    # complete card can replace it in-place rather than append
                    # another identical header (C1 regression #81).
                    start_block = render_tool_start_line(event, self.theme)
                    self.blocks.append(start_block)
                    key = event.tool_call_id or f"index:{event.index}"
                    self._pending_tool_calls[key] = (dict(event.args), start_block)

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
                    key = event.tool_call_id or f"index:{event.index}"
                    pending = self._pending_tool_calls.pop(key, None)
                    start_args, start_block = (
                        pending if pending is not None else ({}, None)
                    )
                    done_block = render_tool_done_line(
                        event, self.theme, args=start_args
                    )
                    if start_block is not None:
                        # Replace by identity: multiple parallel calls may
                        # share the same tool name and detail, but never the
                        # same start-block object.
                        position = next(
                            (
                                i
                                for i, block in enumerate(self.blocks)
                                if block is start_block
                            ),
                            None,
                        )
                        if position is None:
                            # Defensive fallback for a transcript cleared
                            # while a tool was running.
                            self.blocks.append(done_block)
                        else:
                            self.blocks[position] = done_block
                    else:
                        self.blocks.append(done_block)

            case AgentStepDoneEvent():
                if self.current_turn is not None:
                    self.current_turn.current_step += 1
                    self.current_turn.run_cost_usd += event.step.cost_usd
                # Live header (I-88): the controller already folded this
                # step into session_metrics and _context_tokens, so the
                # header's cost/token indicators are fresh — repaint now
                # (throttled to the streaming cadence) so the numbers
                # track the run instead of jumping at the end.
                self.touch_streaming()

            case AgentDoneEvent():
                self.cancel_stream_throttle()
                turn = self.current_turn
                if turn is not None:
                    self._last_reasoning = turn.reasoning
                self._freeze_current_text(turn)
                self.current_turn = None
                line = render_done_line(event, self.theme)
                if line is not None:
                    self.blocks.append(line)

            case AgentErrorEvent():
                self.cancel_stream_throttle()
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

    def print_history(self, path: list[Message], tail: int | None = None) -> None:
        """Replay conversation history into the chat pane.

        By default the *full* path is rendered (#56): the chat window can
        only scroll through what lands in ``blocks``, so a fixed tail
        made older messages unreachable after resuming. Pass ``tail`` to
        deliberately truncate (a "N messages above" rule is shown).
        """
        self.blocks.append(render_history(path, self.theme, tail=tail))
        self._touch()

    def notify(self, kind: str, message: str) -> None:
        self.blocks.append(render_notice(kind, message, self.theme))
        self._touch()

    def on_subagent_progress(self, progress: object | None) -> None:
        """Store the live metrics tracker for the active sub-agent call.

        The panel renders from it while that call's sub-agents run;
        ``None`` clears it when the call ends. By the time a sub-agent
        tool can notify, ``AgentStartEvent`` has created the in-flight
        turn, so the tracker is never lost.
        """
        turn = self.current_turn
        if turn is not None:
            turn.subagent_progress = progress
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
            turn.subagent_tasks,
            turn.subagent_frame,
            self.theme,
            progress=turn.subagent_progress,
        )


__all__ = ["FullScreenSink", "CurrentTurn", "REPAINT_INTERVAL_SECONDS"]

#: Target repaint interval while streaming tokens (~16fps). Token events
#: coalesce into at most one scheduled repaint per interval.
REPAINT_INTERVAL_SECONDS = 0.06

# Rotating labels for the *thinking* phase of the activity line. Kept short
# (they share one line with the spinner) and deliberately light on tone —
# the goal is "still working" feedback, not decoration. Edit freely: this
# list is the single source of truth.
_THINKING_PHRASES = (
    "Thinking…",
    "Pondering the problem…",
    "Reading between the lines…",
    "Weighing the options…",
    "Tracing the logic…",
    "Chewing on that…",
    "Mapping the next move…",
    "Almost there…",
)

# How many activity ticks (~0.12 s each) per thinking-phrase rotation,
# i.e. roughly one new phrase every 2.5 s.
_THINKING_PHRASE_TICKS = 21
