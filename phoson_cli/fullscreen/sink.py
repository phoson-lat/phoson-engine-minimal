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
import datetime
from typing import Any
from dataclasses import dataclass
from collections.abc import Callable

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
    AgentToolComposingEvent,
)
from phoson_llm.schemas import Message

from ..theme import Theme
from ..animations import SPINNER_FRAMES
from ..formatting import (
    ReplayToolGroup,
    ToolRenderRegistry,
    tool_icon,
    tool_verb,
    render_notice,
    history_blocks,
    render_done_line,
    render_user_turn,
    render_error_notice,
    render_tool_done_line,
    render_streaming_panel,
    render_tool_start_line,
    subagent_tasks_from_args,
    render_reasoning_expanded,
    render_tool_group_summary,
    render_reasoning_collapsed,
    render_subagent_start_line,
)
from ..tool_history import next_tool_detail, is_valid_tool_detail
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
    # T-3: monotonic-clock time the turn first emitted reasoning. Lets the
    # collapsed ``thought Ns`` line report how long the model thought.
    # None until the first AgentReasoningEvent.
    reasoning_since: float | None = None
    running_tool: bool = False
    # Tool name being *composed* by the LLM right now (I-128): set by
    # AgentToolComposingEvent, cleared by AgentToolStartEvent. Rendered
    # on the in-chat activity line ("✍ writing file…") instead of the
    # generic thinking phrases. Lives on the turn (not in ``blocks``) so
    # a stream that dies mid-composing leaves no orphan line behind.
    composing_tool: str = ""
    subagent_tasks: list[str] | None = None
    subagent_frame: int = 0
    # Live metrics for the sub-agent panel (E2): the per-call tracker the
    # sub-agent tool created and pushed via ``on_subagent_progress``. The
    # panel renders from it when present; without it the table falls back
    # to the static "waiting" cells.
    subagent_progress: object | None = None
    activity_frame: int = 0
    # Monotonic-clock timestamp of when the turn entered its current
    # *thinking* episode (T-5): rendered as "Thinking {n}s". None while
    # the turn is in any other phase; re-armed on every re-entry so the
    # counter restarts from 0 after a tool call or a streamed-text gap.
    thinking_since: float | None = None


@dataclass(eq=False)
class _ToolCard:
    """One regular tool call inside a :class:`_ToolGroup`.

    ``event`` stays ``None`` while the call runs; ``block`` is the object
    currently standing for it (the start line, then its done card). While
    the group is folded into a summary the card block is off-transcript.
    """

    args: dict[str, Any]
    theme: Theme
    block: object
    event: AgentToolDoneEvent | None = None
    #: False for user ``!`` commands: their output is what was asked for,
    #: so ``summary`` shows them in full instead of folding them.
    foldable: bool = True


@dataclass(eq=False)
class _ToolGroup:
    """A run of consecutive tool calls between two pieces of text (a tramo).

    ``/details`` re-renders every group in place: ``full``/``headers`` show
    one card per call, ``summary`` folds a *closed* group into one line.
    The open (in-flight) group always shows cards so live progress stays
    visible. ``foldable=False`` groups (user ``!`` commands) never fold.
    """

    cards: list[_ToolCard]
    closed: bool = False
    foldable: bool = True
    summary: object | None = None

    def finished(self) -> list[_ToolCard]:
        return [card for card in self.cards if card.event is not None]


@dataclass
class _SessionViewSnapshot:
    blocks: list[object]
    plugin_blocks: dict[str, object]
    current_turn: CurrentTurn | None
    last_reasoning: str
    reasoning_blocks: list[tuple[object, str, bool, Theme]]
    expanded_reasoning_nodes: set[str]
    pending_tool_calls: dict[str, _ToolCard]
    tool_groups: list[_ToolGroup]
    open_tool_group: _ToolGroup | None
    error_notice: object | None
    stream_event: bool
    dirty: bool
    app_state: object | None


class FullScreenSink:
    """``AgentEventSink`` implementation for the full-screen ``PhosonApp``.

    Args:
        on_invalidate: Called after every state mutation (bound to the
            running ``Application.invalidate``).
        theme: The active theme, for building renderables.
    """

    def __init__(
        self,
        on_invalidate,
        theme: Theme,
        show_reasoning: bool = True,
        on_session_snapshot=None,
        on_session_reset=None,
        on_session_restore=None,
        on_persistence_required=None,
        on_persistence_released=None,
        tool_detail: str = "full",
    ) -> None:
        self._on_invalidate = on_invalidate
        self._on_session_snapshot = on_session_snapshot
        self._on_session_reset = on_session_reset
        self._on_session_restore = on_session_restore
        self._on_persistence_required = on_persistence_required
        self._on_persistence_released = on_persistence_released
        self.theme = theme
        self.session_id: str | None = None
        self.dirty = True
        self.show_reasoning_default: bool = show_reasoning
        self._tool_render_registry = ToolRenderRegistry({})

        self.blocks: list[object] = []
        # Builds the empty-state welcome screen (set by the app, which owns
        # the version/model/session-list facts); ``None`` = one-line hint.
        self.welcome_provider: Callable[[], object] | None = None
        self._plugin_blocks: dict[str, object] = {}
        self.current_turn: CurrentTurn | None = None
        self._last_reasoning: str = ""
        # T-3: the finalized collapsed-reasoning block for each finished
        # turn, as (block, reasoning_text, expanded). The first entry is
        # the *newest* turn. ``expand_reasoning`` replaces the matching
        # collapsed line with the full text in place; once a turn is
        # expanded, later Ctrl+T presses expand the next one.
        self._reasoning_blocks: list[tuple[object, str, bool, Theme]] = []
        # Node identity is authoritative for one-shot expansion. Reasoning text
        # is not unique: resumed and live nodes may legitimately contain the
        # same content and must remain independently expandable.
        self._expanded_reasoning_nodes: set[str] = set()
        # In-flight regular tool calls keyed by tool_call_id (C1). Done
        # events don't carry args, and the done card REPLACES the start
        # line so each call renders as exactly one card (appending would
        # duplicate the header).
        self._pending_tool_calls: dict[str, _ToolCard] = {}
        # Streaming repaint throttle state (see touch_streaming).
        self._last_stream_repaint: float = 0.0
        self._stream_repaint_pending: asyncio.TimerHandle | None = None
        # True while the event currently being processed is a token/reasoning
        # event (I-84): its repaint goes through the throttled
        # touch_streaming() instead of the unconditional _touch() at the end
        # of on_event, which would otherwise defeat the throttle.
        self._stream_event: bool = False
        # The pending single-line error notice block (I-83). Repeated
        # failures overwrite it in place instead of stacking panels; the
        # next successful run drops it. Tracked by identity, not index:
        # /details folding inserts and removes blocks before it.
        self._error_notice: object | None = None
        # T-7: every run of tool calls (live and replayed), so /details can
        # re-render them in place; the in-flight run is ``_open_tool_group``.
        self._tool_groups: list[_ToolGroup] = []
        self._open_tool_group: _ToolGroup | None = None
        # /details level: full · headers · summary (see tool_history).
        self.tool_detail: str = (
            tool_detail if is_valid_tool_detail(tool_detail) else "full"
        )

    def set_tool_render_registry(self, registry: ToolRenderRegistry) -> None:
        """Apply the active controller's isolated plugin visual specs."""
        self._tool_render_registry = registry

    def set_theme(self, theme: Theme) -> None:
        """Theme future blocks while preserving finalized append-only blocks.

        Finalized Rich renderables intentionally retain their original palette;
        rebuilding all historical block kinds would require storing a parallel
        semantic event transcript. Mutable/live content and newly emitted blocks
        use the new theme immediately.
        """
        self.theme = theme

    @property
    def _error_notice_idx(self) -> int | None:
        """Current index of the pending error notice, if still shown."""
        notice = self._error_notice
        if notice is None:
            return None
        for index, block in enumerate(self.blocks):
            if block is notice:
                return index
        return None

    def set_tool_details(self, mode: str | None = None) -> str:
        """T-7: switch the tool-call detail level, returning the new one.

        ``None`` cycles ``full → headers → summary``. Every tool run in the
        transcript — live or replayed — is re-rendered in place: blocks are
        replaced by identity, so nothing else in the transcript moves.
        """
        if mode is None:
            mode = next_tool_detail(self.tool_detail)
        if not is_valid_tool_detail(mode):
            raise ValueError(f"unknown tool detail level {mode!r}")
        self.tool_detail = mode
        for group in self._tool_groups:
            self._render_tool_group(group)
        self._touch()
        return mode

    # ── Tool runs (/details) ──────────────────────────────────────────────

    def _card_block(self, card: _ToolCard) -> object:
        assert card.event is not None
        return render_tool_done_line(
            card.event,
            card.theme,
            args=card.args,
            registry=self._tool_render_registry,
            collapsed=self.tool_detail == "headers"
            or (self.tool_detail == "summary" and card.foldable),
        )

    def _index_of(self, block: object) -> int | None:
        for index, candidate in enumerate(self.blocks):
            if candidate is block:
                return index
        return None

    def _render_tool_group(self, group: _ToolGroup) -> None:
        """Re-render *group* at the current level, in place."""
        finished = group.finished()
        if not finished:
            return
        fold = group.closed and group.foldable and self.tool_detail == "summary"
        if fold:
            summary = render_tool_group_summary(
                [(card.event, card.args) for card in finished],  # type: ignore[misc]
                finished[0].theme,
                self._tool_render_registry,
            )
            anchor = group.summary if group.summary is not None else finished[0].block
            position = self._index_of(anchor)
            if position is None:
                return  # transcript cleared while the run was remembered
            if group.summary is None:
                card_blocks = {id(card.block) for card in finished}
                before = sum(1 for b in self.blocks[:position] if id(b) in card_blocks)
                self.blocks = [b for b in self.blocks if id(b) not in card_blocks]
                position -= before
                self.blocks.insert(position, summary)
            else:
                self.blocks[position] = summary
            group.summary = summary
            return
        if group.summary is not None:
            # Unfold: the cards take the summary's place, in call order.
            position = self._index_of(group.summary)
            group.summary = None
            if position is None:
                return
            for card in finished:
                card.block = self._card_block(card)
            self.blocks[position : position + 1] = [card.block for card in finished]
            return
        for card in finished:
            position = self._index_of(card.block)
            card.block = self._card_block(card)
            if position is not None:
                self.blocks[position] = card.block

    def _close_tool_group(self) -> None:
        """End the current tool run (text, a user turn or the turn's end)."""
        group = self._open_tool_group
        if group is None:
            return
        self._open_tool_group = None
        group.closed = True
        if self.tool_detail == "summary":
            self._render_tool_group(group)

    def publish_plugin_block(self, block_id: str, block: object) -> None:
        """Append a plugin block, namespaced by the caller's stable id."""
        self._plugin_blocks[block_id] = block
        self.blocks.append(block)
        self._touch()

    def replace_plugin_block(self, block_id: str, block: object) -> None:
        """Replace a plugin block in place so TODO/progress cards stay compact."""
        previous = self._plugin_blocks.get(block_id)
        self._plugin_blocks[block_id] = block
        if previous is not None:
            for index, candidate in enumerate(self.blocks):
                if candidate is previous:
                    self.blocks[index] = block
                    self._touch()
                    return
        self.blocks.append(block)
        self._touch()

    def remove_plugin_block(self, block_id: str) -> None:
        block = self._plugin_blocks.pop(block_id, None)
        if block is not None:
            self.blocks = [
                candidate for candidate in self.blocks if candidate is not block
            ]
            self._touch()

    def add_bash_card(
        self,
        command: str,
        result: str,
        *,
        duration_ms: int = 0,
        error: str | None = None,
    ) -> None:
        """Append a completed bash tool card to the transcript (T-12 ``!``).

        Reuses :func:`render_tool_done_line` with a synthetic
        ``AgentToolDoneEvent`` so the card looks exactly like a real bash
        tool call. It is recorded as its own tool run so ``/details`` can
        re-collapse/expand it like any other finished tool; unlike agent
        runs it never folds into a summary line.

        Args:
            command: The shell command that was run (shown in the header).
            result: The combined stdout+stderr output.
            duration_ms: Wall-clock duration of the run.
            error: Set to a non-None string to render an ✗ card (e.g.
                "denied by the user"). When set, ``result`` is ignored.
        """
        event = AgentToolDoneEvent(
            tool_name="bash",
            result=result,
            error=error,
            duration_ms=duration_ms,
        )
        self._close_tool_group()
        card = _ToolCard(
            args={"command": command},
            theme=self.theme,
            block=None,
            event=event,
            foldable=False,
        )
        card.block = self._card_block(card)
        self.blocks.append(card.block)
        self._tool_groups.append(_ToolGroup(cards=[card], closed=True, foldable=False))
        self._touch()

    def _touch(self) -> None:
        self.dirty = True
        self._on_invalidate()

    def touch_streaming(self) -> None:
        """Dirty-flag update throttled during live token streaming.

        Tokens can arrive hundreds of times per second; with the ANSI
        block cache each frame is cheap, but waking prompt_toolkit's
        renderer that often is still wasted work. Coalesce repaints to
        ~10fps (REPAINT_INTERVAL_SECONDS, I-84) while a turn is in flight.
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

            def repaint() -> None:
                self._stream_repaint_pending = None
                self._last_stream_repaint = time.monotonic()
                self._touch()

            self._stream_repaint_pending = loop.call_later(max(delay, 0.0), repaint)

    def cancel_stream_throttle(self) -> None:
        """Drop any pending throttled repaint timer (turn finished)."""
        if self._stream_repaint_pending is not None:
            self._stream_repaint_pending.cancel()
            self._stream_repaint_pending = None
        self._last_stream_repaint = 0.0
        self._stream_event = False

    def drop_error_notice(self) -> None:
        """Remove the pending single-line error notice from the transcript (I-83).

        Called when a run completes successfully: the warning should not
        leave a ghost line behind once the retry worked. Also called by
        the app after transcript resets (``clear()`` / rewind re-draws)
        to drop the reference together with the blocks. A notice no
        longer in ``blocks`` is treated as "no notice".
        """
        idx = self._error_notice_idx
        self._error_notice = None
        if idx is not None:
            del self.blocks[idx]

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
            or turn.composing_tool
        ):
            self.current_turn = None
            self._touch()

    def activity_text(self) -> str:
        """Human-readable phase for the transient chat activity line.

        The *thinking* phase shows the elapsed wait (T-5): a real number
        that ticks up every second instead of rotating through stock
        phrases ("Pondering the problem…"), which read as decoration.
        The other phases are informational and stay fixed: they describe
        the real state, not a mood.
        """
        turn = self.current_turn
        if turn is None:
            return ""
        if turn.subagent_tasks:
            return "Running subagents…"
        # Composing beats streaming: the model may have written text
        # before the tool call, and the verb is the freshest signal.
        if turn.composing_tool:
            return (
                f"{tool_icon(turn.composing_tool, self._tool_render_registry)} "
                f"{tool_verb(turn.composing_tool, self._tool_render_registry)}…"
            )
        if turn.running_tool:
            return "Running tool…"
        if turn.content:
            return "Streaming…"
        if turn.thinking_since is None:
            # A fresh turn with no provider feedback yet: count from 0.
            turn.thinking_since = time.monotonic()
        elapsed = time.monotonic() - turn.thinking_since
        return f"Thinking {int(elapsed)}s"

    def activity_frame(self) -> str:
        """Current spinner glyph for the active turn (empty when idle)."""
        turn = self.current_turn
        if turn is None:
            return ""
        index = turn.activity_frame % len(SPINNER_FRAMES)
        return SPINNER_FRAMES[index]

    def tick_activity_frame(self) -> bool:
        """Advance the in-chat spinner; return whether a repaint is due.

        The glyph animates in three phases and is frozen in two (I-84 CPU
        budget):

        Animates:
        - *thinking* — the spinner is the only feedback; it animates while
          the "Thinking {n}s" label (T-5) piggybacks on the same repaints
          and its seconds tick up on the wall clock.
        - *composing* (I-128) — the "✍ writing file…" line is static text,
          so the spinning glyph keeps a slow args generation from looking
          frozen.
        - *running tool* — the start card is static until the tool finishes
          and the streamed text is already frozen, so without the glyph
          nothing moves during a long ``bash``/build; the activity line is
          the only feedback. The repaint is cheap (blocks are cached and
          there is no streaming panel — ``content`` is empty), matching
          the cost of the subagent panel animation.

        Frozen:
        - *streaming* — the growing text IS the feedback and the streaming
          panel repaints on the token throttle already.
        - *subagents* — the subagent panel animates itself via
          ``tick_subagent_frame``; the spinner would be redundant churn.
        """
        turn = self.current_turn
        if turn is None:
            return False
        if turn.subagent_tasks or (turn.content and not turn.composing_tool):
            return False
        turn.activity_frame += 1
        return True

    # ── AgentEventSink ───────────────────────────────────────────────────

    def on_user_message(self, text: str, message: Message) -> None:
        self._close_tool_group()
        # #212: stamp the message with its send time (local).
        self.blocks.append(
            render_user_turn(text, self.theme, at=datetime.datetime.now())
        )
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
                self.cancel_stream_throttle()
                # No meta line here (cli_abel-style: the response starts
                # directly with the assistant label, no model/session line —
                # the header bar already shows the active model).
                self.current_turn = CurrentTurn(
                    model=event.model,
                    message_count=event.message_count,
                    max_steps=event.max_iterations,
                    show_reasoning=self.show_reasoning_default,
                    reasoning_since=None,
                )

            case AgentTokenEvent():
                if self.current_turn is not None:
                    self.current_turn.content += event.content
                    self._stream_event = True

            case AgentReasoningEvent():
                if self.current_turn is not None:
                    if self.current_turn.reasoning_since is None:
                        self.current_turn.reasoning_since = time.monotonic()
                    self.current_turn.reasoning += event.content
                    self._stream_event = True

            case AgentToolComposingEvent():
                # I-128: live feedback while the model still generates the
                # call. Idempotent label update on the turn (the activity
                # line re-renders from it); AgentToolStartEvent below
                # replaces it with the real tool card. The trailing
                # _touch() repaints — composing arrives throttled to
                # ~4/s, so no extra cadence control is needed.
                turn = self.current_turn
                if turn is not None and event.tool_name:
                    turn.composing_tool = event.tool_name

            case AgentToolStartEvent():
                turn = self.current_turn
                self._freeze_current_text(turn)
                # The composing label is done: the start card (or subagent
                # start line) is the feedback from here on (I-128).
                if turn is not None:
                    turn.composing_tool = ""
                    # T-5: whatever thinking episode ended with this tool
                    # call is over — the next one (model generating after
                    # the tool) counts from 0.
                    turn.thinking_since = None
                if event.tool_name in {"agent", "agents"}:
                    # Subagent lines keep their own layout, outside any run.
                    self._close_tool_group()
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
                    start_block = render_tool_start_line(
                        event, self.theme, self._tool_render_registry
                    )
                    self.blocks.append(start_block)
                    card = _ToolCard(
                        args=dict(event.args), theme=self.theme, block=start_block
                    )
                    group = self._open_tool_group
                    if group is None:
                        group = _ToolGroup(cards=[])
                        self._open_tool_group = group
                        self._tool_groups.append(group)
                    group.cards.append(card)
                    key = event.tool_call_id or f"index:{event.index}"
                    self._pending_tool_calls[key] = card

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
                    card = self._pending_tool_calls.pop(key, None)
                    if card is None:
                        # Done without a start (defensive): file it under
                        # the current run like any other call.
                        card = _ToolCard(args={}, theme=self.theme, block=None)
                        group = self._open_tool_group
                        if group is None:
                            group = _ToolGroup(cards=[])
                            self._open_tool_group = group
                            self._tool_groups.append(group)
                        group.cards.append(card)
                    start_block = card.block
                    card.event = event
                    card.block = self._card_block(card)
                    # Replace by identity: multiple parallel calls may share
                    # the same tool name and detail, but never the same
                    # start-block object. Not found = the transcript was
                    # cleared while the tool ran (or there was no start).
                    position = (
                        self._index_of(start_block) if start_block is not None else None
                    )
                    if position is None:
                        self.blocks.append(card.block)
                    else:
                        self.blocks[position] = card.block

            case AgentStepDoneEvent():
                if self.current_turn is not None:
                    self.current_turn.current_step += 1
                    self.current_turn.run_cost_usd += event.step.cost_usd
                # Live header (I-88): the controller already folded this
                # step into session_metrics and _context_tokens, so the
                # header's cost/token indicators are fresh — repaint now
                # (throttled to the streaming cadence) so the numbers
                # track the run instead of jumping at the end.
                self._stream_event = True

            case AgentDoneEvent():
                self.protect_persistence()
                self.cancel_stream_throttle()
                self._close_tool_group()
                turn = self.current_turn
                if turn is not None:
                    self._last_reasoning = turn.reasoning
                self._freeze_current_text(turn)
                self._finalize_reasoning(turn)
                self.current_turn = None
                # The run succeeded: a previously failed retry is done,
                # so the pending error notice disappears (I-83).
                self.drop_error_notice()
                line = render_done_line(event, self.theme)
                if line is not None:
                    self.blocks.append(line)

            case AgentErrorEvent():
                self.protect_persistence()
                self.cancel_stream_throttle()
                self._close_tool_group()
                turn = self.current_turn
                if turn is not None:
                    self._last_reasoning = turn.reasoning
                self._freeze_current_text(turn)
                self._finalize_reasoning(turn)
                self.current_turn = None
                # Single-line notice, overwritten in place on each failed
                # retry instead of stacking a panel per attempt (I-83).
                notice = render_error_notice(event, self.theme)
                idx = self._error_notice_idx
                if idx is not None:
                    self.blocks[idx] = notice
                else:
                    self.blocks.append(notice)
                self._error_notice = notice

        # I-84: this used to be an unconditional _touch() that defeated the
        # touch_streaming() throttle — every token invalidated regardless
        # of the 10 fps cadence. Token/reasoning events are the only cases
        # where the throttled (possibly trailing-scheduled) repaint
        # replaces the immediate one; all other events keep their
        # immediate invalidation.
        if self._stream_event:
            self._stream_event = False
            self.touch_streaming()
        else:
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
        # Text after tool calls ends their run (the "tramo" /details folds).
        self._close_tool_group()
        self.blocks.append(render_streaming_panel(turn.content, "", False, self.theme))
        turn.content = ""
        # T-5: the waiting episode ended — streaming takes over the label,
        # and any later thinking episode counts from 0 again.
        turn.thinking_since = None

    def _finalize_reasoning(self, turn: CurrentTurn | None) -> None:
        """T-3: collapse a finished turn's reasoning into one muted line.

        Called at every turn end (success, error, cancel). If the turn
        produced reasoning, a single ``thought Ns`` line is appended to
        the transcript (no ``Panel``) and recorded in
        :attr:`_reasoning_blocks` so Ctrl+T can expand it *in place* later.
        The elapsed seconds come from the first ``AgentReasoningEvent``;
        when there is no timestamp the seconds are omitted.
        """
        if turn is None:
            return
        if not turn.reasoning:
            return
        elapsed: float | None = None
        if turn.reasoning_since is not None:
            elapsed = time.monotonic() - turn.reasoning_since
        turn.reasoning_since = None
        block = render_reasoning_collapsed(elapsed, self.theme)
        self.blocks.append(block)
        # Newest first: Ctrl+T expands the most recent un-expanded turn.
        self._reasoning_blocks.insert(0, (block, turn.reasoning, False, self.theme))

    def flush_line(self) -> None:
        """Freeze the in-flight turn (cancel/error paths before a terminal event).

        Called by ``SessionController`` right before ``capture_partial_reasoning``
        on cancellation — captures the reasoning here too (rather than
        relying on that follow-up call) since finalizing clears
        ``current_turn``, and there would otherwise be nothing left for
        ``capture_partial_reasoning`` to read.
        """
        self.cancel_stream_throttle()
        # The turn is over (terminal event or cancellation): any tool call
        # that never delivered its done event is unrecoverable, so release
        # its bookkeeping before the early return — a turn already cleared
        # by ``AgentErrorEvent`` must not pin those blocks forever.
        self._release_pending_tool_calls()
        self._close_tool_group()
        turn = self.current_turn
        if turn is None:
            return
        self._last_reasoning = turn.reasoning
        self._freeze_current_text(turn)
        self._finalize_reasoning(turn)
        self.current_turn = None
        self._touch()

    def _release_pending_tool_calls(self) -> None:
        """Forget in-flight tool-call bookkeeping once the turn is over.

        ``_pending_tool_calls`` holds the card (start args and transcript
        block) of each regular tool call so its done event can replace the live start
        line in place. A call that is cancelled before its done event leaves
        that entry behind, pinning a block that can never be replaced. The
        visible start line stays in the transcript as the partial-progress
        record; only the bookkeeping is dropped.
        """
        self._pending_tool_calls.clear()

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

    def expand_reasoning(self, node_id: str, reasoning: str) -> bool:
        """Ctrl+T post-turn: expand a collapsed reasoning line in place (T-3).

        Finds the newest finished turn whose collapsed ``thought Ns`` line
        has not yet been expanded and swaps that line for the full
        reasoning text — in place, with **no** ``Panel``. A turn is
        expanded at most once by ``node_id`` (the transcript is append-only,
        mirroring the classic REPL's one-shot Ctrl+T). Live turns resolve text
        from :attr:`_reasoning_blocks`; resumed turns use ``reasoning``.
        """
        if node_id in self._expanded_reasoning_nodes:
            return False
        for index, (block, text, expanded, originating_theme) in enumerate(
            self._reasoning_blocks
        ):
            if expanded:
                continue
            if block not in self.blocks:
                # Transcript was cleared (Ctrl+L) or rebuilt (rewind): the
                # collapsed line is gone, so this record is stale — skip.
                continue
            position = self.blocks.index(block)
            self.blocks[position] = render_reasoning_expanded(
                text if text else reasoning, originating_theme
            )
            self._reasoning_blocks[index] = (
                self.blocks[position],
                text,
                True,
                originating_theme,
            )
            self._touch()
            self._expanded_reasoning_nodes.add(node_id)
            return True
        # Resumed reasoning has no live collapsed-line record. Append it in
        # the same unboxed style after all newer live records are expanded.
        # Node-level one-shot tracking in state_cycles prevents duplicates.
        if reasoning:
            self.blocks.append(render_reasoning_expanded(reasoning, self.theme))
            self._touch()
            self._expanded_reasoning_nodes.add(node_id)
            return True
        return False

    def clear_reasoning_state(self) -> None:
        """Drop collapsed-line and node-identity records on transcript reset."""
        self._reasoning_blocks.clear()
        self._expanded_reasoning_nodes.clear()

    def clear_transcript_bookkeeping(self) -> None:
        """Drop finished bookkeeping that pins a cleared transcript in memory.

        Ctrl+L empties ``blocks`` while a turn may still be streaming, so
        the active turn (``current_turn``), its in-flight tool calls
        (``_pending_tool_calls``) and the stream throttle are deliberately
        left intact. What is released here is the *finished* state that
        would otherwise keep the deleted blocks alive for the rest of the
        session: the published plugin blocks and the remembered
        ``/details`` tool runs (the in-flight run included: its blocks are
        gone, and late done cards append on their own). Reasoning records are dropped by
        :meth:`clear_reasoning_state` and the error-notice index by
        :meth:`drop_error_notice`.
        """
        self._plugin_blocks.clear()
        self._tool_groups.clear()
        self._open_tool_group = None

    def protect_persistence(self) -> None:
        """Mark the current app operation's remaining save as required."""
        if self._on_persistence_required is not None:
            self._on_persistence_required()

    def release_persistence(self) -> None:
        """Drop the requirement once the save it guarded has settled.

        Used around the *mid-turn* checkpoints: the write is
        durability-critical while it is in flight (an interrupted save loses
        the steps it was persisting), but once it has landed the turn is
        cancellable again like any other, so Ctrl+C must go back to
        cancelling instead of deferring exit until the turn runs to the end.
        A *failed* checkpoint never reaches this: the failure is itself a
        persistence failure and stays protected.
        """
        if self._on_persistence_released is not None:
            self._on_persistence_released()

    def snapshot_session_view(self) -> object:
        """Capture transcript and app-owned state for transactional replacement."""
        app_state = (
            self._on_session_snapshot()
            if self._on_session_snapshot is not None
            else None
        )
        return _SessionViewSnapshot(
            blocks=list(self.blocks),
            plugin_blocks=dict(self._plugin_blocks),
            current_turn=self.current_turn,
            last_reasoning=self._last_reasoning,
            reasoning_blocks=list(self._reasoning_blocks),
            expanded_reasoning_nodes=set(self._expanded_reasoning_nodes),
            pending_tool_calls=dict(self._pending_tool_calls),
            tool_groups=list(self._tool_groups),
            open_tool_group=self._open_tool_group,
            error_notice=self._error_notice,
            stream_event=self._stream_event,
            dirty=self.dirty,
            app_state=app_state,
        )

    def reset_session_view(self) -> None:
        """Clear all transcript state before displaying another session."""
        self.cancel_stream_throttle()
        self.blocks.clear()
        self._plugin_blocks.clear()
        self.current_turn = None
        self._last_reasoning = ""
        self._reasoning_blocks.clear()
        self._expanded_reasoning_nodes.clear()
        self._pending_tool_calls.clear()
        self._tool_groups.clear()
        self._open_tool_group = None
        self._error_notice = None
        self._stream_event = False
        self.dirty = True
        if self._on_session_reset is not None:
            self._on_session_reset()
        else:
            self._on_invalidate()

    def restore_session_view(self, snapshot: object) -> None:
        """Restore transcript and app-owned state after a failed replacement."""
        if not isinstance(snapshot, _SessionViewSnapshot):
            raise TypeError("invalid fullscreen session view snapshot")
        self.blocks = snapshot.blocks
        self._plugin_blocks = snapshot.plugin_blocks
        self.current_turn = snapshot.current_turn
        self._last_reasoning = snapshot.last_reasoning
        self._reasoning_blocks = snapshot.reasoning_blocks
        self._expanded_reasoning_nodes = snapshot.expanded_reasoning_nodes
        self._pending_tool_calls = snapshot.pending_tool_calls
        self._tool_groups = snapshot.tool_groups
        self._open_tool_group = snapshot.open_tool_group
        self._error_notice = snapshot.error_notice
        self._stream_event = snapshot.stream_event
        self.dirty = snapshot.dirty
        if self._on_session_restore is not None:
            self._on_session_restore(snapshot.app_state)
        else:
            self._on_invalidate()

    def set_session(self, session_id: str) -> None:
        self.session_id = session_id

    def on_session_title(self) -> None:
        """Repaint chrome after a background session-title update.

        Optional hook (not part of the ``AgentEventSink`` protocol): the
        header reads ``tree.title`` each frame, so the async title landing
        only needs an invalidate — deliberately *not* a visible notice.
        """
        self._touch()

    def print_history(
        self,
        path: list[Message],
        tail: int | None = None,
        timestamps: "list[datetime.datetime | None] | None" = None,
        tool_durations: "dict[str, int] | None" = None,
    ) -> None:
        """Replay conversation history into the chat pane.

        By default the *full* path is rendered (#56): the chat window can
        only scroll through what lands in ``blocks``, so a fixed tail
        made older messages unreachable after resuming. Pass ``tail`` to
        deliberately truncate (a "N messages above" rule is shown).

        ``timestamps`` (#212) optionally carries each message's local time so
        the replay shows a date/time per message (aligned with *path* before
        any tail trim). ``tool_durations`` maps ``tool_call_id`` to the
        persisted duration of each call.

        Each message lands as its own block and every tool run is
        registered like a live one, so ``/details`` re-renders replayed
        tool calls in place too.
        """
        self._close_tool_group()
        for item in history_blocks(
            path,
            self.theme,
            tail=tail,
            timestamps=timestamps,
            tool_durations=tool_durations,
        ):
            if not isinstance(item, ReplayToolGroup):
                self.blocks.append(item)
                continue
            group = _ToolGroup(
                cards=[
                    _ToolCard(args=args, theme=self.theme, block=None, event=event)
                    for event, args in item.calls
                ],
                closed=True,
            )
            if self.tool_detail == "summary":
                group.summary = render_tool_group_summary(
                    item.calls, self.theme, self._tool_render_registry
                )
                self.blocks.append(group.summary)
            for card in group.cards:
                card.block = self._card_block(card)
                if group.summary is None:
                    self.blocks.append(card.block)
            self._tool_groups.append(group)
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

#: Target repaint interval while streaming tokens (I-84: ~10 fps; 16 fps
#: of text changing character by character is indistinguishable to the
#: eye and cut ~40% of repaints). Token events coalesce into at most one
#: scheduled repaint per interval.
REPAINT_INTERVAL_SECONDS = 0.10
