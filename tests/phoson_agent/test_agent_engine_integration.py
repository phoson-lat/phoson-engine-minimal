from collections.abc import AsyncIterator

import pytest

from phoson_agent.agent import AgentEngine
from phoson_agent.tool import tool
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_agent.models import (
    AgentTool,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_llm.chats.base import BaseLLMChat


class FakeToolChat(BaseLLMChat):
    def __init__(self) -> None:
        self._iteration = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self._iteration += 1

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        if self._iteration == 1:
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_fake_weather_1",
                tool_name="get_weather",
                args={"city": "Queretaro", "country": "MX"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=120, output=28),
                cost_usd=0.00042,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return

        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=180, output=46),
            cost_usd=0.00067,
            cost_known=True,
        )
        yield LLMDoneEvent(
            content="En Queretaro esta soleado, 27C, humedad moderada.",
            has_tool_calls=False,
        )


class FakeErrorChat(BaseLLMChat):
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield ErrorEvent(message="provider timeout", code="timeout", retryable=True)


def get_weather(args: dict) -> dict:
    city = args.get("city", "unknown")
    country = args.get("country", "unknown")
    return {
        "city": city,
        "country": country,
        "condition": "sunny",
        "temperature_c": 27,
        "humidity": "moderate",
    }


def build_tools() -> list[AgentTool]:
    return [
        AgentTool(
            name="get_weather",
            description="Returns current weather by city and country.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "country": {"type": "string"},
                },
                "required": ["city"],
            },
            handler=get_weather,
        )
    ]


@tool(inject=["safe_mode"])
def run_shell(command: str, safe_mode: bool = False) -> str:
    return f"cmd={command} safe={safe_mode}"


@pytest.mark.asyncio
async def test_run_integration_fake_tool_chat() -> None:
    engine = AgentEngine(chat=FakeToolChat(), tools=build_tools(), phoson_weight=1.2)
    result = await engine.run(
        messages=[Message(role="user", content="Que clima hace en Queretaro?")],
        config=ModelConfig(model="fake-demo-model", max_tokens=256),
    )

    assert "Queretaro" in result.final_content
    assert result.total_cost_usd == pytest.approx(0.00109, abs=1e-9)
    assert result.total_credits == pytest.approx(0.001308, abs=1e-9)
    assert [step.kind for step in result.steps] == ["llm", "tool", "llm"]
    assert result.steps[1].tool_name == "get_weather"
    assert result.steps[1].error is None
    assert len(result.history) == 4


@pytest.mark.asyncio
async def test_stream_integration_emits_tool_and_done_events() -> None:
    engine = AgentEngine(chat=FakeToolChat(), tools=build_tools(), phoson_weight=1.2)
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="Que clima hace en Queretaro?")],
            config=ModelConfig(model="fake-demo-model", max_tokens=256),
        )
    ]

    assert any(isinstance(event, AgentToolStartEvent) for event in events)
    assert any(isinstance(event, AgentToolDoneEvent) for event in events)
    assert any(isinstance(event, AgentStepDoneEvent) for event in events)

    done_events = [event for event in events if isinstance(event, AgentDoneEvent)]
    assert len(done_events) == 1
    assert "Queretaro" in done_events[0].result.final_content


@pytest.mark.asyncio
async def test_run_raises_when_llm_emits_error() -> None:
    engine = AgentEngine(chat=FakeErrorChat(), tools=build_tools(), phoson_weight=1.2)

    with pytest.raises(
        RuntimeError, match=r"Agent error \(timeout\): provider timeout"
    ):
        await engine.run(
            messages=[Message(role="user", content="test")],
            config=ModelConfig(model="fake-demo-model", max_tokens=128),
        )


@pytest.mark.asyncio
async def test_stream_emits_agent_error_when_llm_fails() -> None:
    engine = AgentEngine(chat=FakeErrorChat(), tools=build_tools(), phoson_weight=1.2)
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="test")],
            config=ModelConfig(model="fake-demo-model", max_tokens=128),
        )
    ]

    error_events = [event for event in events if isinstance(event, AgentErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "timeout"
    assert error_events[0].retryable is True


@pytest.mark.asyncio
async def test_run_executes_decorated_tool_with_context_injection() -> None:
    class FakeInjectedToolChat(BaseLLMChat):
        async def stream(
            self,
            messages: list[Message],
            config: ModelConfig,
            tools: list[ToolDefinition] | None = None,
        ) -> AsyncIterator[LLMEvent]:
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_shell_1",
                tool_name="run_shell",
                args={"command": "git log -1 --oneline"},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)

    engine = AgentEngine(chat=FakeInjectedToolChat(), tools=[run_shell])
    engine.context.extra["safe_mode"] = True

    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="revisa cambios")],
            config=ModelConfig(model="fake-demo-model", max_tokens=128),
        )
    ]

    tool_done = next(event for event in events if isinstance(event, AgentToolDoneEvent))
    assert tool_done.result == "cmd=git log -1 --oneline safe=True"
