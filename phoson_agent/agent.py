"""
Module for the main agent engine.
"""

import json
import asyncio
import datetime
from typing import Any
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningTokenEvent,
)
from phoson_agent.models import (
    RunStep,
    AgentTool,
    AgentEvent,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.middleware import LLMCallNext, AgentMiddleware


def _now_utc() -> datetime.datetime:
    """Returns the current date and time in UTC."""
    return datetime.datetime.now(datetime.UTC)


def _duration_ms(started_at: datetime.datetime, ended_at: datetime.datetime) -> int:
    """Calculates the duration in milliseconds between two timestamps."""
    return int((ended_at - started_at).total_seconds() * 1000)


def _to_result_text(value: str | dict[str, Any]) -> str:
    """Converts a tool result to a text string."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True)


@dataclass
class AgentEngine:
    """
    Main engine for running LLM-based agents
    with support for tools and middleware.
    """

    chat: BaseLLMChat
    tools: list[AgentTool]
    middlewares: list[AgentMiddleware] = field(default_factory=list)
    context: AgentContext = field(default_factory=AgentContext)
    phoson_weight: float = 1.0
    max_iterations: int = 12
    _history: list[Message] = field(default_factory=list, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initializes the tool map by name."""
        self._tools_by_name: dict[str, AgentTool] = {
            tool.name: tool for tool in self.tools
        }

    def get_partial_history(self) -> list[Message]:
        """Returns the current message history."""
        return list(self._history)

    def is_running(self) -> bool:
        """Checks if the agent is currently running."""
        return self._running

    async def _notify_middlewares(self, event: AgentEvent) -> None:
        """Notifies all middlewares about an agent event."""
        for middleware in self.middlewares:
            await middleware.on_agent_event(event)

    async def _prepare_event(self, event: AgentEvent) -> AgentEvent:
        """Prepares an event, notifying the middlewares."""
        await self._notify_middlewares(event)
        return event

    async def _apply_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        """Applies middlewares before calling the LLM."""
        updated = messages
        for middleware in self.middlewares:
            updated = await middleware.on_before_llm(updated, config)
        return updated

    def _build_llm_call_chain(
        self,
        tool_definitions: list[ToolDefinition],
    ) -> LLMCallNext:
        """Builds the middleware execution chain for the LLM call."""

        async def base_call(
            messages: list[Message],
            config: ModelConfig,
        ) -> AsyncIterator[LLMEvent]:
            async for event in self.chat.stream(messages, config, tool_definitions):  # pyright: ignore[reportGeneralTypeIssues]
                yield event

        call_next: LLMCallNext = base_call
        for middleware in reversed(self.middlewares):
            previous = call_next

            def make_wrapped(
                mw: AgentMiddleware,
                nxt: LLMCallNext,
            ) -> LLMCallNext:
                async def wrapped(
                    messages: list[Message],
                    config: ModelConfig,
                ) -> AsyncIterator[LLMEvent]:
                    async for event in mw.wrap_llm_call(nxt, messages, config):
                        yield event

                return wrapped

            call_next = make_wrapped(middleware, previous)

        return call_next

    async def _apply_before_tool(
        self,
        call: ToolCallEvent,
    ) -> ToolCallEvent | None:
        """Applies middlewares before executing a tool."""
        current: ToolCallEvent | None = call
        for middleware in self.middlewares:
            if current is None:
                return None
            current = await middleware.on_before_tool(current)
        return current

    async def _apply_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        """Applies middlewares after executing a tool."""
        updated = result
        for middleware in self.middlewares:
            updated = await middleware.on_after_tool(call, updated, error)
        return updated

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[AgentEvent]:
        """
        Executes the agent and streams events as they occur.
        """
        if self._running:
            raise RuntimeError("AgentEngine is already running.")

        input_snapshot = list(messages)
        history = list(messages)
        self._history = history
        self._running = True
        steps: list[RunStep] = []
        total_cost_usd = 0.0
        total_credits = 0.0
        final_content = ""

        tool_definitions = [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.tools
        ]
        llm_call = self._build_llm_call_chain(tool_definitions)

        try:
            yield await self._prepare_event(
                AgentStartEvent(
                    model=config.model,
                    message_count=len(messages),
                    max_iterations=self.max_iterations,
                )
            )

            for _ in range(self.max_iterations):
                history = await self._apply_before_llm(history, config)
                self._history = history

                llm_started = _now_utc()
                usage_event: UsageEvent | None = None
                done_event: LLMDoneEvent | None = None
                error_event: ErrorEvent | None = None
                tool_calls: list[ToolCallEvent] = []

                async for event in llm_call(history, config):
                    if isinstance(event, TokenEvent):
                        yield await self._prepare_event(
                            AgentTokenEvent(content=event.content)
                        )
                    elif isinstance(event, ReasoningTokenEvent):
                        yield await self._prepare_event(
                            AgentReasoningEvent(content=event.content)
                        )
                    elif isinstance(event, ToolCallEvent):
                        tool_calls.append(event)
                    elif isinstance(event, UsageEvent):
                        usage_event = event
                    elif isinstance(event, LLMDoneEvent):
                        done_event = event
                    elif isinstance(event, ErrorEvent):
                        error_event = event
                    elif isinstance(event, LLMStartEvent):
                        continue

                llm_ended = _now_utc()
                llm_cost = usage_event.cost_usd if usage_event else 0.0
                llm_credits = llm_cost * self.phoson_weight
                total_cost_usd += llm_cost
                total_credits += llm_credits

                llm_step = RunStep(
                    kind="llm",
                    started_at=llm_started,
                    ended_at=llm_ended,
                    duration_ms=_duration_ms(llm_started, llm_ended),
                    model=config.model,
                    usage=usage_event.usage if usage_event else None,
                    cost_usd=llm_cost,
                    credits=llm_credits,
                    error=(
                        f"[{error_event.code}] {error_event.message}"
                        if error_event and error_event.code
                        else error_event.message
                        if error_event
                        else None
                    ),
                    payload={
                        "input_tokens": usage_event.usage.input if usage_event else 0,
                        "output_tokens": usage_event.usage.output if usage_event else 0,
                    },
                )
                steps.append(llm_step)
                yield await self._prepare_event(AgentStepDoneEvent(step=llm_step))

                if error_event:
                    yield await self._prepare_event(
                        AgentErrorEvent(
                            message=error_event.message,
                            code=error_event.code,
                            retryable=error_event.retryable,
                        )
                    )
                    return

                if not done_event:
                    yield await self._prepare_event(
                        AgentErrorEvent(
                            message="LLM stream finished without LLMDoneEvent.",
                            code="llm_protocol",
                            retryable=False,
                        )
                    )
                    return

                final_content = done_event.content

                if not done_event.has_tool_calls:
                    history.append(
                        Message(role="assistant", content=done_event.content)
                    )
                    result = AgentRunResult(
                        final_content=final_content,
                        history=history,
                        input_messages=input_snapshot,
                        steps=steps,
                        total_cost_usd=total_cost_usd,
                        total_credits=total_credits,
                    )
                    yield await self._prepare_event(AgentDoneEvent(result=result))
                    return

                if not tool_calls:
                    yield await self._prepare_event(
                        AgentErrorEvent(
                            message="LLM indicated tool calls but emitted none.",
                            code="llm_protocol",
                            retryable=False,
                        )
                    )
                    return

                assistant_blocks: list[TextBlock | ToolUseBlock] = []
                if done_event.content:
                    assistant_blocks.append(TextBlock(text=done_event.content))

                for call in tool_calls:
                    assistant_blocks.append(
                        ToolUseBlock(
                            tool_call_id=call.tool_call_id,
                            tool_name=call.tool_name,
                            args=call.args,
                        )
                    )

                history.append(Message(role="assistant", content=assistant_blocks))

                for call_idx, original_call in enumerate(tool_calls):
                    result_committed = False
                    try:
                        call = await self._apply_before_tool(original_call)

                        if call is None:
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
                            result_committed = True

                            blocked_step = RunStep(
                                kind="tool",
                                started_at=_now_utc(),
                                ended_at=_now_utc(),
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
                            yield await self._prepare_event(
                                AgentStepDoneEvent(step=blocked_step)
                            )
                            continue

                        yield await self._prepare_event(
                            AgentToolStartEvent(
                                index=call.index,
                                tool_call_id=call.tool_call_id,
                                tool_name=call.tool_name,
                                args=call.args,
                                label=(
                                    "subagent"
                                    if call.tool_name == "agent"
                                    else "subagents"
                                    if call.tool_name == "agents"
                                    else None
                                ),
                            )
                        )

                        tool_started = _now_utc()
                        tool_error: str | None = None
                        result_text = ""
                        error_flag = False

                        tool = self._tools_by_name.get(call.tool_name)
                        if not tool:
                            tool_error = f"Tool '{call.tool_name}' is not registered."
                            result_text = tool_error
                            error_flag = True
                        else:
                            try:
                                tool_result = tool.handler(call.args, self.context)

                                if asyncio.iscoroutine(tool_result):
                                    tool_result = await tool_result

                                if not isinstance(tool_result, (str, dict)):
                                    raise TypeError(
                                        "Tool handler must return str, dict, "
                                        "or awaitable of those types."
                                    )

                                result_text = _to_result_text(tool_result)
                            except Exception as exc:
                                tool_error = str(exc)
                                result_text = tool_error
                                error_flag = True

                        result_text = await self._apply_after_tool(
                            call=call,
                            result=result_text,
                            error=error_flag,
                        )

                        tool_ended = _now_utc()
                        tool_step = RunStep(
                            kind="tool",
                            started_at=tool_started,
                            ended_at=tool_ended,
                            duration_ms=_duration_ms(tool_started, tool_ended),
                            tool_name=call.tool_name,
                            tool_call_id=call.tool_call_id,
                            error=tool_error,
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
                        result_committed = True

                        yield await self._prepare_event(
                            AgentToolDoneEvent(
                                index=call.index,
                                tool_call_id=call.tool_call_id,
                                tool_name=call.tool_name,
                                result=result_text,
                                error=tool_error,
                                duration_ms=tool_step.duration_ms,
                                label=(
                                    "subagent"
                                    if call.tool_name == "agent"
                                    else "subagents"
                                    if call.tool_name == "agents"
                                    else None
                                ),
                            )
                        )
                        yield await self._prepare_event(
                            AgentStepDoneEvent(step=tool_step)
                        )
                    except asyncio.CancelledError:
                        start_idx = call_idx + 1 if result_committed else call_idx
                        for pending_call in tool_calls[start_idx:]:
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
                        raise

            yield await self._prepare_event(
                AgentErrorEvent(
                    message=(
                        "Agent reached "
                        f"max_iterations={self.max_iterations} without a final answer."
                    ),
                    code="max_iterations",
                    retryable=False,
                )
            )
            return
        finally:
            self._running = False

    async def run(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AgentRunResult:
        """Executes the agent until completion and returns the result."""
        async for event in self.stream(messages, config):
            if isinstance(event, AgentDoneEvent):
                return event.result
            if isinstance(event, AgentErrorEvent):
                code = event.code or "unknown"
                raise RuntimeError(f"Agent error ({code}): {event.message}")

        raise RuntimeError("Agent stream finished without AgentDoneEvent.")

    def run_sync(self, messages: list[Message], config: ModelConfig) -> AgentRunResult:
        """Executes the agent synchronously."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.run(messages, config))
        finally:
            loop.close()
