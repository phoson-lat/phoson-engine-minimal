"""Tool execution for the agent loop.

The :class:`ToolRunner` owns the logic for invoking tool handlers,
honouring middleware ``on_before_tool``/``on_after_tool`` hooks,
handling blocked tools and synthesising cancellation results so that
the conversation history stays well-formed even when a run is
interrupted mid-tool.

This module is private to ``phoson_agent``. It is composed by
:class:`phoson_agent.agent.AgentEngine` rather than being exported
directly.
"""

import asyncio
import inspect
import logging
import datetime
from dataclasses import dataclass
from collections.abc import Callable, Iterator, Awaitable, AsyncIterator

from phoson_llm.schemas import (
    Message,
    ImageBlock,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_agent.models import (
    RunStep,
    AgentTool,
    AgentEvent,
    ImageToolResult,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_agent.context import AgentContext
from phoson_agent._internals import (
    now_utc,
    duration_ms,
    subagent_label,
    to_result_text,
)
from phoson_agent.exceptions import PhosonAgentError

logger = logging.getLogger(__name__)

# Result text recorded when the model's tool call was cut off by its token
# budget (stop_reason == max_tokens) or arrived with malformed JSON (the
# adapter's ``_raw`` fallback). Its argument JSON is incomplete, so the handler
# is *not* invoked — the model is told why and how to recover (#178/F-13).
TRUNCATED_TOOL_CALL_RESULT = (
    "Tool call NOT executed: its argument JSON is incomplete or could not be "
    "parsed (the model likely hit its token budget mid-call). Retry the same "
    "tool with shorter, complete arguments, or split the work across multiple "
    "smaller calls."
)

# Result recorded when a middleware's ``on_before_tool`` returned ``None``.
BLOCKED_TOOL_RESULT = "Tool execution blocked by middleware."

# Default message recorded for tool calls left unrun by an abnormal exit
# (user cancellation or an unhandled exception escaping the runner).
CANCELLED_TOOL_RESULT = "Tool execution cancelled by user."

# Result template for an unhandled exception that escapes the tool handler or
# a middleware hook. The exception type name is included so the model (and the
# user) can tell a genuine bug from a provider hiccup (#178/F-14).
TOOL_HANDLER_ERROR_TEMPLATE = (
    "Internal error while executing tool '{name}': {exc_type}: {detail}"
)


def _is_unusable_args(args: object) -> bool:
    """True when a tool call's args are not safely dispatchable to a handler.

    Adapters tag a call whose argument JSON is incomplete so the agent loop
    can answer it instead of invoking the handler:

    * ``_truncated`` — the response hit ``max_tokens`` mid tool-call (F-13);
    * ``_raw`` — the accumulated JSON did not parse, so the adapter fell back
      to the opaque ``{"_raw": ...}`` marker.

    Either way, calling the handler with those args would raise an opaque
    ``TypeError`` (or act on partial data). The presence of *either* key means
    the call must be answered with an actionable error result, not dispatched.
    """
    if not isinstance(args, dict):
        return False
    return "_truncated" in args or "_raw" in args


# Type aliases for the middleware-applying callbacks the engine injects.
# Keeping them typed keeps the runner decoupled from ``AgentEngine``.
BeforeToolFn = Callable[[ToolCallEvent], Awaitable[ToolCallEvent | None]]
AfterToolFn = Callable[[ToolCallEvent, str, bool], Awaitable[str]]
PrepareEventFn = Callable[[AgentEvent], Awaitable[AgentEvent]]


class ToolRunner:
    """Executes tool calls produced by the LLM during one iteration.

    Args:
        tools_by_name: Lookup table of registered tools.
        context: The :class:`AgentContext` shared with the engine.
        apply_before_tool: Callable that runs every middleware
            ``on_before_tool`` hook. May return ``None`` to indicate the
            call should be blocked.
        apply_after_tool: Callable that runs every middleware
            ``on_after_tool`` hook to optionally rewrite the result.
        prepare_event: Callable that notifies middlewares about an
            agent event before it is yielded.
    """

    def __init__(
        self,
        *,
        tools_by_name: dict[str, AgentTool],
        context: AgentContext,
        apply_before_tool: BeforeToolFn,
        apply_after_tool: AfterToolFn,
        prepare_event: PrepareEventFn,
    ) -> None:
        self._tools_by_name = tools_by_name
        self._context = context
        self._apply_before_tool = apply_before_tool
        self._apply_after_tool = apply_after_tool
        self._prepare_event = prepare_event

    async def execute(
        self,
        *,
        tool_calls: list[ToolCallEvent],
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Execute every tool call of one LLM turn, keeping history well-formed.

        Calls are processed in *batches*: a maximal run of consecutive calls
        to :attr:`~phoson_agent.models.AgentTool.parallel_safe` tools forms
        one batch whose handlers run concurrently; every other call is a
        batch of one. Within a batch the middleware ``on_before_tool`` hooks
        still run one at a time in call order (permission prompts never
        overlap) and results are committed to ``history`` in call order.

        Invariants:

        * every ``tool_use`` gets exactly one matching ``tool_result`` before
          this generator exits — on success, on handler/middleware errors
          (converted to error results, F-14) and on any abnormal exit
          (cancellation, a consumer closing the stream), where unanswered
          calls are backfilled with :data:`CANCELLED_TOOL_RESULT`;
        * image results are appended only after *all* tool results of the
          turn: providers require the tool results to directly follow the
          assistant turn (OpenAI rejects a user message between ``tool``
          messages; Anthropic requires ``tool_result`` blocks to lead the
          user turn).
        """
        progress = _Progress()
        images: list[ImageBlock] = []
        try:
            for batch in self._batches(tool_calls):
                async for event in self._execute_batch(
                    batch, history, steps, images, progress
                ):
                    yield event
        except BaseException:
            self._fill_cancelled_results(history, tool_calls[progress.answered :])
            raise
        finally:
            for image in images:
                # ToolResultBlock.result is a plain string — an image cannot
                # live inside it, so it rides in its own user message, after
                # the last tool result of the turn.
                history.append(Message(role="user", content=[image]))

    def _is_parallel_safe(self, call: ToolCallEvent) -> bool:
        # getattr: duck-typed tool objects (not AgentTool) are accepted
        # elsewhere in the runner and simply run sequentially.
        tool = self._tools_by_name.get(call.tool_name)
        return getattr(tool, "parallel_safe", False) is True

    def _batches(
        self, tool_calls: list[ToolCallEvent]
    ) -> Iterator[list[ToolCallEvent]]:
        """Group consecutive parallel-safe calls; everything else runs alone."""
        batch: list[ToolCallEvent] = []
        for call in tool_calls:
            if self._is_parallel_safe(call):
                batch.append(call)
                continue
            if batch:
                yield batch
                batch = []
            yield [call]
        if batch:
            yield batch

    async def _execute_batch(
        self,
        batch: list[ToolCallEvent],
        history: list[Message],
        steps: list[RunStep],
        images: list[ImageBlock],
        progress: "_Progress",
    ) -> AsyncIterator[AgentEvent]:
        """Admit, run and commit one batch of tool calls (see :meth:`execute`)."""
        # 1. Admission: middleware hooks, one call at a time, in order.
        admissions: list[ToolCallEvent | _Rejected] = []
        for original in batch:
            admissions.append(await self._admit(original))
        admitted = [a for a in admissions if isinstance(a, ToolCallEvent)]

        for call in admitted:
            yield await self._prepare_event(
                AgentToolStartEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args,
                    label=subagent_label(call.tool_name),
                )
            )

        # 2. Run the handlers — concurrently only when every admitted call
        # (after middleware rewrites) is still parallel-safe.
        concurrent = len(admitted) > 1 and all(
            self._is_parallel_safe(call) for call in admitted
        )
        if concurrent:
            runs = list(
                await asyncio.gather(
                    *(self._run_handler(call, threaded=True) for call in admitted)
                )
            )
        else:
            runs = [await self._run_handler(call, threaded=False) for call in admitted]

        # 3. Commit in call order: after-hooks, history, steps, events.
        pending_runs = iter(runs)
        for admission in admissions:
            if isinstance(admission, _Rejected):
                async for event in self._commit_rejected(
                    admission, history, steps, progress
                ):
                    yield event
            else:
                async for event in self._commit_run(
                    next(pending_runs), history, steps, images, progress
                ):
                    yield event

    async def _admit(self, original_call: ToolCallEvent) -> "ToolCallEvent | _Rejected":
        """Run the ``on_before_tool`` chain and decide whether to dispatch.

        Returns the (possibly rewritten) call to run, or a :class:`_Rejected`
        describing the result to record instead.
        """
        try:
            call = await self._apply_before_tool(original_call)
        except PhosonAgentError as exc:
            # Permission refusals (ToolBlockedError) surface as an actionable
            # tool result — the model sees *why* the call was refused — and
            # the structured decision rides along in the step payload so the
            # OTel plugin can export the intents (#227).
            return _Rejected(
                call=original_call,
                result=str(exc),
                code="permission_denied",
                decision=getattr(exc, "decision", None),
            )
        except Exception as exc:  # noqa: BLE001 - F-14, a buggy hook must not kill the run
            logger.error(
                "Unhandled exception in on_before_tool for %s: %r",
                original_call.tool_name,
                exc,
            )
            return _Rejected(
                call=original_call,
                result=TOOL_HANDLER_ERROR_TEMPLATE.format(
                    name=original_call.tool_name,
                    exc_type=type(exc).__name__,
                    detail=str(exc),
                ),
                code="middleware_error",
            )

        if call is None:
            return _Rejected(
                call=original_call,
                result=BLOCKED_TOOL_RESULT,
                code="blocked_by_middleware",
            )

        # F-13: a tool call whose argument JSON is incomplete — either the
        # adapter marked it ``_truncated`` (cut by max_tokens mid-call) or
        # fell back to ``_raw`` (unparseable JSON) — must NOT be dispatched:
        # that would raise an opaque ``TypeError`` or act on partial args.
        if _is_unusable_args(original_call.args):
            return _Rejected(
                call=original_call,
                result=TRUNCATED_TOOL_CALL_RESULT,
                code="tool_call_truncated",
            )
        return call

    async def _run_handler(self, call: ToolCallEvent, *, threaded: bool) -> "_Run":
        """Invoke one handler, timing it; never raises (except cancellation)."""
        started = now_utc()
        try:
            result_text, error_text, error_flag, image = await self._invoke_handler(
                call, threaded=threaded
            )
        except Exception as exc:  # noqa: BLE001 - F-14, deliberate catch-all
            result_text, error_text, error_flag, image = _internal_error(call, exc)
        return _Run(
            call=call,
            result_text=result_text,
            error_text=error_text,
            error_flag=error_flag,
            image=image,
            started=started,
            ended=now_utc(),
        )

    async def _commit_run(
        self,
        run: "_Run",
        history: list[Message],
        steps: list[RunStep],
        images: list[ImageBlock],
        progress: "_Progress",
    ) -> AsyncIterator[AgentEvent]:
        """Apply ``on_after_tool`` and record an executed call."""
        call = run.call
        result_text, error_text, error_flag, image = (
            run.result_text,
            run.error_text,
            run.error_flag,
            run.image,
        )
        try:
            result_text = await self._apply_after_tool(call, result_text, error_flag)
        except Exception as exc:  # noqa: BLE001 - F-14, deliberate catch-all
            result_text, error_text, error_flag, image = _internal_error(call, exc)

        tool_step = RunStep(
            kind="tool",
            started_at=run.started,
            ended_at=run.ended,
            duration_ms=duration_ms(run.started, run.ended),
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            error=error_text,
            payload={"args": call.args, "result": result_text},
        )
        steps.append(tool_step)
        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=call.tool_call_id,
                        result=result_text,
                        error=error_flag,
                    )
                ],
            )
        )
        progress.answered += 1
        if image is not None:
            images.append(image)

        yield await self._prepare_event(
            AgentToolDoneEvent(
                index=call.index,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                result=result_text,
                error=error_text,
                duration_ms=tool_step.duration_ms,
                label=subagent_label(call.tool_name),
            )
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=tool_step))

    async def _commit_rejected(
        self,
        rejected: "_Rejected",
        history: list[Message],
        steps: list[RunStep],
        progress: "_Progress",
    ) -> AsyncIterator[AgentEvent]:
        """Record a call that was not dispatched (blocked, refused, truncated
        or failed admission) as an error result with a stable step code."""
        call = rejected.call
        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=call.tool_call_id,
                        result=rejected.result,
                        error=True,
                    )
                ],
            )
        )
        progress.answered += 1

        payload: dict[str, object] = {"args": call.args, "result": rejected.result}
        to_dict = getattr(rejected.decision, "to_dict", None)
        if callable(to_dict):
            payload["permission"] = to_dict()

        now = now_utc()
        step = RunStep(
            kind="tool",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            error=rejected.code,
            payload=payload,
        )
        steps.append(step)

        yield await self._prepare_event(
            AgentToolDoneEvent(
                index=call.index,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                result=rejected.result,
                error=rejected.code,
                duration_ms=0,
            )
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=step))

    async def _invoke_handler(
        self,
        call: ToolCallEvent,
        *,
        threaded: bool = False,
    ) -> tuple[str, str | None, bool, ImageBlock | None]:
        """Invoke a tool handler.

        Returns ``(result_text, error_text, error_flag, image)``. Catches
        every exception raised by the handler and surfaces it as a
        tool-level error so the agent loop can inform the LLM and continue.
        With ``threaded=True`` a synchronous handler runs in a worker thread
        so concurrent (parallel-safe) calls do not serialize on the loop.
        """
        tool = self._tools_by_name.get(call.tool_name)
        if tool is None:
            error_text = f"Tool '{call.tool_name}' is not registered."
            return error_text, error_text, True, None

        try:
            if threaded and not inspect.iscoroutinefunction(tool.handler):
                tool_result = await asyncio.to_thread(
                    tool.handler, call.args, self._context
                )
            else:
                tool_result = tool.handler(call.args, self._context)
            if asyncio.iscoroutine(tool_result):
                tool_result = await tool_result

            if isinstance(tool_result, ImageToolResult):
                return tool_result.text, None, False, tool_result.image

            if not isinstance(tool_result, (str, dict)):
                raise TypeError(
                    "Tool handler must return str, dict, ImageToolResult, or "
                    "awaitable of those types."
                )

            return to_result_text(tool_result), None, False, None
        except Exception as exc:
            error_text = str(exc)
            return error_text, error_text, True, None

    def _fill_cancelled_results(
        self,
        history: list[Message],
        pending_calls: list[ToolCallEvent],
    ) -> None:
        """Append synthetic cancelled tool_result blocks for unrun calls."""
        for pending_call in pending_calls:
            history.append(
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_call_id=pending_call.tool_call_id,
                            result=CANCELLED_TOOL_RESULT,
                            error=True,
                        )
                    ],
                )
            )


def _internal_error(
    call: ToolCallEvent, exc: Exception
) -> tuple[str, str | None, bool, ImageBlock | None]:
    """Error result for an exception escaping a handler or middleware hook.

    The exception type name is included so the model (and the user) can tell
    a genuine bug from a provider hiccup (#178/F-14).
    """
    logger.error("Unhandled exception executing tool %s: %r", call.tool_name, exc)
    text = TOOL_HANDLER_ERROR_TEMPLATE.format(
        name=call.tool_name, exc_type=type(exc).__name__, detail=str(exc)
    )
    return text, text, True, None


@dataclass
class _Progress:
    """How many calls of the turn already have a result in history.

    Results are committed strictly in call order, so this index is exactly
    where the cancellation backfill must start.
    """

    answered: int = 0


@dataclass
class _Rejected:
    """A call answered without running its handler."""

    call: ToolCallEvent
    result: str
    code: str
    decision: object | None = None


@dataclass
class _Run:
    """The raw outcome of one handler invocation, before ``on_after_tool``."""

    call: ToolCallEvent
    result_text: str
    error_text: str | None
    error_flag: bool
    image: ImageBlock | None
    started: datetime.datetime
    ended: datetime.datetime
