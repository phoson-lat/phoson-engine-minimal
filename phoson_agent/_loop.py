"""Per-iteration agent loop.

The :class:`AgentLoop` runs a single iteration of the ReAct cycle:

  1. Stream the LLM through the middleware chain, demultiplexing the
     typed events into an :class:`LLMStepOutcome` and yielding the
     user-facing token/reasoning events as they arrive.
  2. Build a :class:`RunStep` summarising the call (cost, usage, error).
  3. Emit a control sentinel for cost accounting in the outer loop.
  4. If the LLM produced tool calls, hand off to a :class:`ToolRunner`;
     otherwise emit an ``IterationFinal`` sentinel with the final text.

The loop is intentionally thin: it has no notion of iteration budget,
plugin lifecycle, or running-flag concurrency control. Those concerns
live in :class:`phoson_agent.agent.AgentEngine`, which composes this
loop with a :class:`ToolRunner`.
"""

import time
import datetime
from collections.abc import Callable, AsyncIterator

from phoson_llm.schemas import (
    Message,
    TextBlock,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolCallDeltaEvent,
    ReasoningTokenEvent,
)
from phoson_agent.models import (
    RunStep,
    AgentEvent,
    AgentErrorEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentReasoningEvent,
    AgentToolComposingEvent,
)
from phoson_agent._internals import (
    IterationCost,
    IterationFinal,
    LLMStepOutcome,
    IterationFailed,
    now_utc,
    duration_ms,
)
from phoson_agent.exceptions import PhosonAgentError
from phoson_agent.middleware import LLMCallNext
from phoson_agent._tool_runner import ToolRunner, PrepareEventFn

# Minimum wall time between :class:`AgentToolComposingEvent` emissions for
# the same tool-call index (I-128). Leading-edge: the first non-empty args
# chunk and the first known tool name always emit; anything in between is
# a heartbeat capped to ~4 events/s. A 200-line ``write_file`` produces
# hundreds of deltas — without this cap every delta would invalidate the
# TUI.
_COMPOSING_THROTTLE_S = 0.25


class _ComposingTracker:
    """Per-index composing state for one LLM stream (I-128).

    Fresh per ``_consume_llm_stream`` call, so no state leaks between
    iterations. ``_now`` is injectable so tests can drive the throttle
    deterministically instead of sleeping.
    """

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        # Resolved at call time (not import time) so tests can patch
        # ``phoson_agent._loop.time.monotonic`` with a fake clock.
        self._now = now if now is not None else time.monotonic
        self.known_name = ""
        self.last_emit_at: float | None = None
        self.last_name_emit_at: float | None = None

    def should_emit(self, tool_name: str, args_chunk: str) -> bool:
        """Decide whether this delta warrants a visible composing event.

        Leading-edge rule: the first non-empty args chunk always emits, and
        the first chunk that carries the tool name always emits (that is
        when the label switches from "composing tool call…" to the real
        verb). After that, further chunks only emit as a heartbeat once
        ``_COMPOSING_THROTTLE_S`` has elapsed since the last emission —
        enough to keep the indicator alive, capped to ~4 events/s.
        """
        if not args_chunk and not tool_name:
            return False
        now = self._now()
        if tool_name and tool_name != self.known_name:
            if self.last_name_emit_at is None:
                return True
            return now - self.last_name_emit_at >= _COMPOSING_THROTTLE_S
        if self.last_emit_at is None:
            return True
        return now - self.last_emit_at >= _COMPOSING_THROTTLE_S

    def record_emitted(self, tool_name: str) -> None:
        """Mark that a composing event was just emitted for this delta."""
        now = self._now()
        self.last_emit_at = now
        if tool_name and tool_name != self.known_name:
            self.known_name = tool_name
            self.last_name_emit_at = now


class AgentLoop:
    """One ReAct iteration: LLM stream + optional tool execution."""

    def __init__(
        self,
        *,
        tool_runner: ToolRunner,
        prepare_event: PrepareEventFn,
        phoson_weight: float,
    ) -> None:
        self._tool_runner = tool_runner
        self._prepare_event = prepare_event
        self._phoson_weight = phoson_weight

    async def run_iteration(
        self,
        *,
        history: list[Message],
        config: ModelConfig,
        llm_call: LLMCallNext,
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Run one iteration and yield a mix of public events and sentinels.

        The outer loop in :class:`AgentEngine` is responsible for
        interpreting the ``IterationCost`` / ``IterationFinal`` /
        ``IterationFailed`` sentinels — see ``_internals.py`` for the
        contract. Public events are forwarded as-is.
        """
        llm_started = now_utc()
        outcome = LLMStepOutcome()

        async for agent_event in self._consume_llm_stream(
            llm_call=llm_call,
            history=history,
            config=config,
            outcome=outcome,
        ):
            yield agent_event

        llm_ended = now_utc()
        llm_step = self._build_llm_step(outcome, config, llm_started, llm_ended)
        steps.append(llm_step)

        yield IterationCost(cost_usd=llm_step.cost_usd, credits=llm_step.credits)
        yield await self._prepare_event(AgentStepDoneEvent(step=llm_step))

        if outcome.error_event is not None:
            yield IterationFailed(
                error_event=AgentErrorEvent(
                    message=outcome.error_event.message,
                    code=outcome.error_event.code,
                    retryable=outcome.error_event.retryable,
                )
            )
            return

        if outcome.done_event is None:
            yield IterationFailed(
                error_event=AgentErrorEvent(
                    message="LLM stream finished without LLMDoneEvent.",
                    code="llm_protocol",
                    retryable=False,
                )
            )
            return

        if not outcome.done_event.has_tool_calls:
            history.append(
                Message(role="assistant", content=outcome.done_event.content)
            )
            yield IterationFinal(
                final_content=outcome.done_event.content,
                truncated=(outcome.done_event.stop_reason == "max_tokens"),
            )
            return

        # F-13: a tool call whose argument JSON is incomplete (cut by
        # max_tokens, or malformed enough for the adapter to fall back to
        # ``_raw``) is tagged by the adapter that saw it (``_truncated`` /
        # ``_raw`` in args). The runner refuses to invoke a handler on such a
        # call and answers it with an actionable error instead — see
        # :meth:`ToolRunner._execute_single`. We do NOT blanket-tag every
        # call on a max_tokens turn: a *complete* tool_use that merely sits
        # at the token boundary (e.g. Anthropic) is still dispatchable, and
        # refusing it would just force the model to re-issue it.
        if not outcome.tool_calls:
            yield IterationFailed(
                error_event=AgentErrorEvent(
                    message="LLM indicated tool calls but emitted none.",
                    code="llm_protocol",
                    retryable=False,
                )
            )
            return

        # Append assistant message with tool_use blocks and dispatch.
        history.append(_build_assistant_message(outcome))

        async for agent_event in self._tool_runner.execute(
            tool_calls=outcome.tool_calls,
            history=history,
            steps=steps,
        ):
            yield agent_event

    # ── Helpers ────────────────────────────────────────────────────────

    async def _consume_llm_stream(
        self,
        *,
        llm_call: LLMCallNext,
        history: list[Message],
        config: ModelConfig,
        outcome: LLMStepOutcome,
    ) -> AsyncIterator[AgentEvent]:
        """Demultiplex the LLM event stream and forward visible events.

        ``outcome`` is mutated in place with the aggregated tool calls,
        usage, done and error events; the loop yields the user-facing
        token and reasoning events as they arrive. Tool-call deltas are
        demoted to (throttled) ``AgentToolComposingEvent`` so front ends
        get live feedback while the model composes the call (I-128).
        """
        composing_trackers: dict[int, _ComposingTracker] = {}
        async for event in llm_call(history, config):
            if isinstance(event, TokenEvent):
                yield await self._prepare_event(AgentTokenEvent(content=event.content))
            elif isinstance(event, ReasoningTokenEvent):
                yield await self._prepare_event(
                    AgentReasoningEvent(content=event.content)
                )
            elif isinstance(event, ToolCallDeltaEvent):
                tracker = composing_trackers.get(event.index)
                if tracker is None:
                    tracker = _ComposingTracker()
                    composing_trackers[event.index] = tracker
                if tracker.should_emit(event.tool_name, event.args_chunk):
                    tracker.record_emitted(event.tool_name)
                    yield await self._prepare_event(
                        AgentToolComposingEvent(
                            index=event.index,
                            tool_call_id="",
                            tool_name=event.tool_name,
                            args_chunk=event.args_chunk,
                        )
                    )
            elif isinstance(event, ToolCallEvent):
                outcome.tool_calls.append(event)
            elif isinstance(event, UsageEvent):
                outcome.usage_event = event
            elif isinstance(event, LLMDoneEvent):
                outcome.done_event = event
            elif isinstance(event, ErrorEvent):
                outcome.error_event = event
            elif isinstance(event, LLMStartEvent):
                continue

    def _build_llm_step(
        self,
        outcome: LLMStepOutcome,
        config: ModelConfig,
        started_at: datetime.datetime,
        ended_at: datetime.datetime,
    ) -> RunStep:
        """Build a :class:`RunStep` summarising the LLM call."""
        usage = outcome.usage_event
        error = outcome.error_event
        cost_usd = usage.cost_usd if usage else 0.0
        credits = cost_usd * self._phoson_weight

        if error is not None and error.code:
            error_text: str | None = f"[{error.code}] {error.message}"
        elif error is not None:
            error_text = error.message
        else:
            error_text = None

        return RunStep(
            kind="llm",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms(started_at, ended_at),
            model=config.model,
            usage=usage.usage if usage else None,
            cost_usd=cost_usd,
            credits=credits,
            error=error_text,
            payload={
                "input_tokens": usage.usage.input if usage else 0,
                "output_tokens": usage.usage.output if usage else 0,
            },
        )


def _build_assistant_message(outcome: LLMStepOutcome) -> Message:
    """Build the assistant message containing text + tool_use blocks."""
    if outcome.done_event is None:
        raise PhosonAgentError("_build_assistant_message called without a done_event")
    blocks: list[TextBlock | ToolUseBlock] = []
    if outcome.done_event.content:
        blocks.append(TextBlock(text=outcome.done_event.content))
    for call in outcome.tool_calls:
        blocks.append(
            ToolUseBlock(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                args=call.args,
            )
        )
    return Message(role="assistant", content=blocks)
