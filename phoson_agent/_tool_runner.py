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
from collections.abc import Callable, Awaitable, AsyncIterator

from phoson_llm.schemas import (
    Message,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_agent.models import (
    RunStep,
    AgentTool,
    AgentEvent,
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
        """Execute every tool call in order with cancellation handling.

        On ``CancelledError`` we backfill synthetic ``tool_result`` blocks
        for the calls that haven't run yet so the next LLM turn does not
        complain about orphaned ``tool_use`` blocks.
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
        call = await self._apply_before_tool(original_call)

        if call is None:
            async for event in self._handle_blocked(original_call, history, steps):
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
        result_text, error_text, error_flag = await self._invoke_handler(call)

        result_text = await self._apply_after_tool(call, result_text, error_flag)

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
    ) -> tuple[str, str | None, bool]:
        """Invoke a tool handler and return ``(result_text, error_text, error_flag)``.

        Catches every exception raised by the handler and surfaces it as a
        tool-level error so the agent loop can inform the LLM and continue.
        """
        tool = self._tools_by_name.get(call.tool_name)
        if tool is None:
            error_text = f"Tool '{call.tool_name}' is not registered."
            return error_text, error_text, True

        try:
            tool_result = tool.handler(call.args, self._context)
            if asyncio.iscoroutine(tool_result):
                tool_result = await tool_result

            if not isinstance(tool_result, (str, dict)):
                raise TypeError(
                    "Tool handler must return str, dict, or awaitable of those types."
                )

            return to_result_text(tool_result), None, False
        except Exception as exc:
            error_text = str(exc)
            return error_text, error_text, True

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
                            result="Tool execution cancelled by user.",
                            error=True,
                        )
                    ],
                )
            )
