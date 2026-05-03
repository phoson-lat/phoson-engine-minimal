import os
import asyncio
from collections.abc import AsyncIterator

from phoson_agent import (
    AgentTool,
    AgentEvent,
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat


class FakeToolChat(BaseLLMChat):
    """
    Fake chat to validate the ReAct loop without depending on real providers.

    Iteration 1: requests get_weather
    Iteration 2: returns final response without tools
    """

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


def get_weather(args: dict) -> dict:
    """Gets the weather for a city and country."""
    city = args.get("city", "unknown")
    country = args.get("country", "unknown")
    return {
        "city": city,
        "country": country,
        "condition": "sunny",
        "temperature_c": 27,
        "humidity": "moderate",
    }


def render_result(label: str, result) -> None:
    """Renders the final agent result."""
    print(f"\n{'=' * 60}")
    print(f"{label}")
    print(f"{'=' * 60}")

    print(f"final_content: {result.final_content}")
    print(f"total_cost_usd: {result.total_cost_usd:.6f}")
    print(f"total_credits: {result.total_credits:.6f}")
    print(f"input_messages: {len(result.input_messages)}")
    print(f"history_messages: {len(result.history)}")

    print("\nsteps:")
    for idx, step in enumerate(result.steps, start=1):
        print(
            f"  {idx}. kind={step.kind} duration_ms={step.duration_ms} "
            f"tool={step.tool_name} error={step.error} payload={step.payload}"
        )


def render_stream_event(event: AgentEvent) -> None:
    """Renders an agent stream event."""
    match event:
        case AgentStartEvent():
            print(
                f"[agent.start] model={event.model} messages={event.message_count} "
                f"max_iterations={event.max_iterations}"
            )
        case AgentTokenEvent():
            print(event.content, end="", flush=True)
        case AgentReasoningEvent():
            print(event.content, end="", flush=True)
        case AgentToolStartEvent():
            print(
                f"\n[agent.tool.start] {event.tool_name} "
                f"id={event.tool_call_id} args={event.args}"
            )
        case AgentToolDoneEvent():
            print(
                f"[agent.tool.done] {event.tool_name} id={event.tool_call_id} "
                f"duration_ms={event.duration_ms} error={event.error}"
            )
        case AgentStepDoneEvent():
            print(
                f"[agent.step.done] kind={event.step.kind} "
                f"duration_ms={event.step.duration_ms}"
            )
        case AgentDoneEvent():
            print("\n[agent.done]")
        case AgentErrorEvent():
            print(f"\n[agent.error] code={event.code} message={event.message}")


async def test_fake_agent_loop() -> None:
    """Tests the agent loop with a fake chat."""
    chat = FakeToolChat()
    tools = [
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

    engine = AgentEngine(chat=chat, tools=tools, phoson_weight=1.2)
    messages = [Message(role="user", content="Que clima hace en Queretaro?")]
    config = ModelConfig(model="fake-demo-model", max_tokens=256)

    result = await engine.run(messages, config)
    render_result("Agent Demo - Fake Chat", result)


async def test_fake_agent_stream() -> None:
    """Tests the agent stream with a fake chat."""
    chat = FakeToolChat()
    engine = AgentEngine(chat=chat, tools=build_tools(), phoson_weight=1.2)
    messages = [Message(role="user", content="Que clima hace en Queretaro?")]
    config = ModelConfig(model="fake-demo-model", max_tokens=256)

    print("\n" + "=" * 60)
    print("Agent Stream Demo - Fake Chat")
    print("=" * 60)
    async for event in engine.stream(messages, config):
        render_stream_event(event)


def build_tools() -> list[AgentTool]:
    """Builds the list of tools for testing."""
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


def build_real_provider_chat() -> tuple[str, BaseLLMChat, ModelConfig] | None:
    """Builds a real provider chat from environment variables."""
    provider = os.environ.get("PHOSON_PROVIDER", "auto").lower()

    if provider in ("openrouter", "auto"):
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if api_key:
            return (
                "openrouter",
                OpenRouterChat(api_key=api_key),
                ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=512),
            )

    if provider in ("openai", "auto"):
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            return (
                "openai",
                OpenAIChat(api_key=api_key),
                ModelConfig(model="gpt-4o-mini", max_tokens=512),
            )

    if provider in ("anthropic", "auto"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            return (
                "anthropic",
                AnthropicChat(api_key=api_key),
                ModelConfig(model="claude-haiku-4-5", max_tokens=512),
            )

    return None


async def test_provider_agent_loop() -> None:
    """Tests the agent loop with a real provider."""
    provider_setup = build_real_provider_chat()
    if not provider_setup:
        print(
            "No provider credentials found. Set OPENROUTER_API_KEY, OPENAI_API_KEY, "
            "or ANTHROPIC_API_KEY "
            "(optional PHOSON_PROVIDER=openrouter|openai|anthropic)."
        )
        print("Falling back to Fake Chat demo.")
        await test_fake_agent_loop()
        return

    provider_name, chat, config = provider_setup
    tools = [
        *build_tools(),
    ]

    engine = AgentEngine(chat=chat, tools=tools, phoson_weight=1.2)
    messages = [Message(role="user", content="Que clima hace en Queretaro?")]

    result = await engine.run(messages, config)
    render_result(f"Agent Demo - {provider_name}", result)


async def test_provider_agent_stream() -> None:
    """Tests the agent stream with a real provider."""
    provider_setup = build_real_provider_chat()
    if not provider_setup:
        print("No provider credentials found. Skipping provider streaming demo.")
        return

    provider_name, chat, config = provider_setup
    engine = AgentEngine(chat=chat, tools=build_tools(), phoson_weight=1.2)
    messages = [Message(role="user", content="Que clima hace en Queretaro?")]

    print("\n" + "=" * 60)
    print(f"Agent Stream Demo - {provider_name}")
    print("=" * 60)
    async for event in engine.stream(messages, config):
        render_stream_event(event)


async def main() -> None:
    """Main function to run tests."""
    tests = {
        "provider_agent_loop": True,
        "provider_agent_stream": True,
        "fake_agent_loop": False,
        "fake_agent_stream": False,
    }

    if tests["provider_agent_loop"]:
        await test_provider_agent_loop()

    if tests["provider_agent_stream"]:
        await test_provider_agent_stream()

    if tests["fake_agent_loop"]:
        await test_fake_agent_loop()

    if tests["fake_agent_stream"]:
        await test_fake_agent_stream()


if __name__ == "__main__":
    asyncio.run(main())
