#!/usr/bin/env python3
"""
Example: phoson-plugin-memory (Redis-backed short-term memory), end to end.

Unlike the old in-process demo (a plain dict that dies with the process),
this exercises the real plugin package (`phoson_plugin_memory`) against a
real Redis instance, and proves persistence survives across two completely
separate AgentEngine instances — which an in-process store never could.

Requires Redis:
    docker compose -f docker-compose.test.yml up -d redis-test
    python examples/plugin_example_memory.py
"""

import asyncio
from collections.abc import AsyncIterator

from phoson_agent import AgentEngine
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

REDIS_URL = "redis://localhost:56379/0"


class WriteMemoryChat(BaseLLMChat):
    """Fake chat: writes a fact to memory, then answers."""

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
                tool_call_id="call_write_1",
                tool_name="memory_write",
                args={"key": "user_name", "value": "Abel"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=80, output=20),
                cost_usd=0.0002,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return

        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=100, output=15),
            cost_usd=0.0002,
            cost_known=True,
        )
        yield LLMDoneEvent(
            content="Noted, I'll remember your name.", has_tool_calls=False
        )


class ReadMemoryChat(BaseLLMChat):
    """Fake chat: reads the fact back from memory, in a brand new process/engine."""

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
                tool_call_id="call_read_1",
                tool_name="memory_read",
                args={"key": "user_name"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=80, output=20),
                cost_usd=0.0002,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return

        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=100, output=15),
            cost_usd=0.0002,
            cost_known=True,
        )
        yield LLMDoneEvent(content="Your name is Abel.", has_tool_calls=False)


async def main() -> None:
    memory_plugin_spec = {
        "name": "phoson-plugin-memory",
        "config": {"redis_url": REDIS_URL, "namespace": "phoson-example"},
    }

    print("=" * 70)
    print("Engine #1 (process A): writes a fact to Redis-backed memory")
    print("=" * 70)
    engine_a = AgentEngine(chat=WriteMemoryChat(), plugins=[memory_plugin_spec])
    try:
        result_a = await engine_a.run(
            [Message(role="user", content="My name is Abel, please remember it.")],
            ModelConfig(model="fake-demo-model", max_tokens=128),
        )
    except Exception as exc:  # pragma: no cover - demo-only guardrail
        print(f"\n Could not reach Redis at {REDIS_URL}: {exc}")
        print(
            "Start it with: docker compose -f docker-compose.test.yml up -d redis-test"
        )
        return
    print(f"final_content: {result_a.final_content}")

    print("\n" + "=" * 70)
    print("Engine #2 (process B, brand new instance): reads the fact back")
    print("=" * 70)
    engine_b = AgentEngine(chat=ReadMemoryChat(), plugins=[memory_plugin_spec])
    result_b = await engine_b.run(
        [Message(role="user", content="What's my name?")],
        ModelConfig(model="fake-demo-model", max_tokens=128),
    )
    print(f"final_content: {result_b.final_content}")

    print(
        "\nMemory survived across two independent AgentEngine instances "
        "because it lives in Redis, not in a Python dict."
    )


if __name__ == "__main__":
    asyncio.run(main())
