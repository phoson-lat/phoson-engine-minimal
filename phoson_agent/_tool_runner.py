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
import logging
from collections.abc import Callable, Awaitable, AsyncIterator

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
        """Execute every tool call in order with backfill on cancellation.

        A ``tool_use`` block the model put in the history must be answered
        with a matching ``tool_result`` before the next LLM turn, or the
        provider rejects the conversation. Handler/middleware exceptions are
        already converted to paired error results inside
        :meth:`_execute_single` (F-14), so the only remaining abnormal exit
        that can leave later calls unrun is ``CancelledError`` (user
        interrupt) — we backfill synthetic results for those and preserve the
        cancel.
        """
        for call_idx, original_call in enumerate(tool_calls):
            committed = False
            try:
                async for agent_event, did_commit in self._execute_single(
                    original_call, history, steps
                ):
                    yield agent_event
                    if did_commit:
                        committed = True
            except asyncio.CancelledError:
                start_idx = call_idx + 1 if committed else call_idx
                self._fill_cancelled_results(history, tool_calls[start_idx:])
                raise

    async def _execute_single(
        self,
        original_call: ToolCallEvent,
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[tuple[AgentEvent, bool]]:
        """Execute a single tool call.

        Yields ``(event, committed)`` pairs. ``committed`` is True for
        the event that follows a successful append to ``history``; the
        cancellation handler in :meth:`execute` uses this flag to know
        whether to re-emit a cancellation result for this tool.
        """
        call: ToolCallEvent | None
        try:
            call = await self._apply_before_tool(original_call)
        except PhosonAgentError as exc:
            # Permission refusals (ToolBlockedError) surface as an
            # actionable tool result — the model sees *why* the call was
            # refused and how to proceed — instead of the generic
            # "blocked by middleware" text.
            async for event in self._handle_refused(
                original_call, history, steps, str(exc)
            ):
                yield event, True
            return

        if call is None:
            async for event in self._handle_blocked(original_call, history, steps):
                yield event, True
            return

        # F-13: a tool call whose argument JSON is incomplete — either the
        # adapter marked it ``_truncated`` (cut by max_tokens mid-call) or
        # fell back to ``_raw`` (unparseable JSON) — must NOT be dispatched to
        # its handler: that would either raise an opaque ``TypeError`` or act
        # on partial args. Answer the tool_use with an actionable error result
        # instead (no Start, no handler) so the model can retry smaller.
        if _is_unusable_args(original_call.args):
            async for event in self._handle_unusable_args(
                original_call, history, steps
            ):
                yield event, True
            return

        yield (
            await self._prepare_event(
                AgentToolStartEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args,
                    label=subagent_label(call.tool_name),
                )
            ),
            False,
        )

        tool_started = now_utc()
        try:
            (
                result_text,
                error_text,
                error_flag,
                result_image,
            ) = await self._invoke_handler(call)
            result_text = await self._apply_after_tool(call, result_text, error_flag)
        except Exception as exc:  # noqa: BLE001 - F-14, deliberate catch-all
            # An unhandled exception in the handler or a middleware hook must
            # NOT escape the loop: it would leave this tool_use without a
            # matching tool_result and corrupt the persisted session, and a
            # single misbehaving hook would kill the whole run. Convert it to
            # an error result (exception type included) so the step and
            # history record *why* the call failed and the model can adapt on
            # the next turn — mirroring the existing handler-error path.
            error_text = TOOL_HANDLER_ERROR_TEMPLATE.format(
                name=call.tool_name, exc_type=type(exc).__name__, detail=str(exc)
            )
            result_text = error_text
            error_flag = True
            result_image = None
            logger.error(
                "Unhandled exception executing tool %s: %r",
                call.tool_name,
                exc,
            )

        tool_ended = now_utc()
        tool_step = RunStep(
            kind="tool",
            started_at=tool_started,
            ended_at=tool_ended,
            duration_ms=duration_ms(tool_started, tool_ended),
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            error=error_text,
            payload={
                "args": call.args,
                "result": result_text,
            },
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
        if result_image is not None:
            # ToolResultBlock.result is a plain string — an image cannot
            # live inside it. Append it as its own user-role message
            # right after the tool result so vision-capable models see
            # the picture, reusing the same content-block path /attach
            # already uses for user-supplied images.
            history.append(Message(role="user", content=[result_image]))

        yield (
            await self._prepare_event(
                AgentToolDoneEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    result=result_text,
                    error=error_text,
                    duration_ms=tool_step.duration_ms,
                    label=subagent_label(call.tool_name),
                )
            ),
            True,
        )
        yield (
            await self._prepare_event(AgentStepDoneEvent(step=tool_step)),
            False,
        )

    async def _invoke_handler(
        self,
        call: ToolCallEvent,
    ) -> tuple[str, str | None, bool, ImageBlock | None]:
        """Invoke a tool handler.

        Returns ``(result_text, error_text, error_flag, image)``.
        Catches every exception raised by the handler and surfaces it as a
        tool-level error so the agent loop can inform the LLM and continue.
        """
        tool = self._tools_by_name.get(call.tool_name)
        if tool is None:
            error_text = f"Tool '{call.tool_name}' is not registered."
            return error_text, error_text, True, None

        try:
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

    async def _handle_blocked(
        self,
        original_call: ToolCallEvent,
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Handle a tool call that was rejected by middleware."""
        cancelled_result = "Tool execution blocked by middleware."
        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=original_call.tool_call_id,
                        result=cancelled_result,
                        error=True,
                    )
                ],
            )
        )

        now = now_utc()
        blocked_step = RunStep(
            kind="tool",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            tool_name=original_call.tool_name,
            tool_call_id=original_call.tool_call_id,
            error="blocked_by_middleware",
            payload={
                "args": original_call.args,
                "result": cancelled_result,
            },
        )
        steps.append(blocked_step)

        yield await self._prepare_event(
            AgentToolDoneEvent(
                index=original_call.index,
                tool_call_id=original_call.tool_call_id,
                tool_name=original_call.tool_name,
                result=cancelled_result,
                error="blocked_by_middleware",
                duration_ms=0,
            )
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=blocked_step))

    async def _handle_refused(
        self,
        original_call: ToolCallEvent,
        history: list[Message],
        steps: list[RunStep],
        message: str,
    ) -> AsyncIterator[AgentEvent]:
        """Handle a call refused by the permission middleware.

        The refusal ``message`` becomes the tool result so the model can
        adapt (and the user sees an actionable explanation), while the
        step records a stable ``permission_denied`` error code.
        """
        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=original_call.tool_call_id,
                        result=message,
                        error=True,
                    )
                ],
            )
        )

        now = now_utc()
        refused_step = RunStep(
            kind="tool",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            tool_name=original_call.tool_name,
            tool_call_id=original_call.tool_call_id,
            error="permission_denied",
            payload={
                "args": original_call.args,
                "result": message,
            },
        )
        steps.append(refused_step)

        yield await self._prepare_event(
            AgentToolDoneEvent(
                index=original_call.index,
                tool_call_id=original_call.tool_call_id,
                tool_name=original_call.tool_name,
                result=message,
                error="permission_denied",
                duration_ms=0,
            )
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=refused_step))

    async def _handle_unusable_args(
        self,
        original_call: ToolCallEvent,
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Handle a tool call whose argument JSON is incomplete or unparseable
        (cut by max_tokens, or the adapter's ``_raw`` fallback). The args are
        NOT passed to the handler — the tool_use is answered with an
        actionable error result so the model can retry with a smaller call
        (#178/F-13)."""
        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=original_call.tool_call_id,
                        result=TRUNCATED_TOOL_CALL_RESULT,
                        error=True,
                    )
                ],
            )
        )

        now = now_utc()
        truncated_step = RunStep(
            kind="tool",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            tool_name=original_call.tool_name,
            tool_call_id=original_call.tool_call_id,
            error="tool_call_truncated",
            payload={
                "args": original_call.args,
                "result": TRUNCATED_TOOL_CALL_RESULT,
            },
        )
        steps.append(truncated_step)

        yield await self._prepare_event(
            AgentToolDoneEvent(
                index=original_call.index,
                tool_call_id=original_call.tool_call_id,
                tool_name=original_call.tool_name,
                result=TRUNCATED_TOOL_CALL_RESULT,
                error="tool_call_truncated",
                duration_ms=0,
            )
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=truncated_step))

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
