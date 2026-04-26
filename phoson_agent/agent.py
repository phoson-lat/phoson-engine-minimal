import json
import asyncio
import inspect
import datetime
from typing import Any
from dataclasses import dataclass
from collections.abc import AsyncIterator

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
from phoson_llm.chats.base import BaseLLMChat


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _duration_ms(started_at: datetime.datetime, ended_at: datetime.datetime) -> int:
    return int((ended_at - started_at).total_seconds() * 1000)


def _to_result_text(value: str | dict[str, Any]) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True)


@dataclass
class AgentEngine:
    chat: BaseLLMChat
    tools: list[AgentTool]
    phoson_weight: float = 1.0
    max_iterations: int = 12

    def __post_init__(self) -> None:
        self._tools_by_name: dict[str, AgentTool] = {
            tool.name: tool for tool in self.tools
        }

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[AgentEvent]:
        input_snapshot = list(messages)
        history = list(messages)
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

        yield AgentStartEvent(
            model=config.model,
            message_count=len(messages),
            max_iterations=self.max_iterations,
        )

        for _ in range(self.max_iterations):
            llm_started = _now_utc()
            usage_event: UsageEvent | None = None
            done_event: LLMDoneEvent | None = None
            error_event: ErrorEvent | None = None
            tool_calls: list[ToolCallEvent] = []

            async for event in self.chat.stream(history, config, tool_definitions):  # pyright: ignore[reportGeneralTypeIssues]
                if isinstance(event, TokenEvent):
                    yield AgentTokenEvent(content=event.content)
                elif isinstance(event, ReasoningTokenEvent):
                    yield AgentReasoningEvent(content=event.content)
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
            yield AgentStepDoneEvent(step=llm_step)

            if error_event:
                yield AgentErrorEvent(
                    message=error_event.message,
                    code=error_event.code,
                    retryable=error_event.retryable,
                )
                return

            if not done_event:
                yield AgentErrorEvent(
                    message="LLM stream finished without LLMDoneEvent.",
                    code="llm_protocol",
                    retryable=False,
                )
                return

            final_content = done_event.content

            if not done_event.has_tool_calls:
                history.append(Message(role="assistant", content=done_event.content))
                result = AgentRunResult(
                    final_content=final_content,
                    history=history,
                    input_messages=input_snapshot,
                    steps=steps,
                    total_cost_usd=total_cost_usd,
                    total_credits=total_credits,
                )
                yield AgentDoneEvent(result=result)
                return

            if not tool_calls:
                yield AgentErrorEvent(
                    message="LLM indicated tool calls but emitted none.",
                    code="llm_protocol",
                    retryable=False,
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

            for call in tool_calls:
                yield AgentToolStartEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args,
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
                        tool_result = tool.handler(call.args)
                        if inspect.isawaitable(tool_result):
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

                yield AgentToolDoneEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    result=result_text,
                    error=tool_error,
                    duration_ms=tool_step.duration_ms,
                )
                yield AgentStepDoneEvent(step=tool_step)

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

        yield AgentErrorEvent(
            message=(
                "Agent reached "
                f"max_iterations={self.max_iterations} without a final answer."
            ),
            code="max_iterations",
            retryable=False,
        )
        return

    async def run(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AgentRunResult:
        async for event in self.stream(messages, config):
            if isinstance(event, AgentDoneEvent):
                return event.result
            if isinstance(event, AgentErrorEvent):
                code = event.code or "unknown"
                raise RuntimeError(f"Agent error ({code}): {event.message}")

        raise RuntimeError("Agent stream finished without AgentDoneEvent.")

    def run_sync(self, messages: list[Message], config: ModelConfig) -> AgentRunResult:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.run(messages, config))
        finally:
            loop.close()
