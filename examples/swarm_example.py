#!/usr/bin/env python3
"""
Functional example for the Agents Swarm plugin (issue #232).

Runs a small research swarm end-to-end **without a real LLM** (it uses a tiny
mock chat), so you can see the whole orchestration — create, message routing,
parallel assign, status, collect — and the exact shape of each result, for
free.

To point it at a real model, replace ``SwarmMockChat`` with e.g.
``phoson_llm.OpenAIChat()`` and give the members real tools (the stubs below
stand in for the host's ``bash``/``web_search``/...).

Run:  uv run python examples/swarm_example.py
"""

import asyncio
from collections.abc import AsyncIterator

from phoson_agent.tool import tool
from phoson_llm.schemas import Message, ModelConfig
from phoson_plugin_swarm import SwarmPlugin
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.schemas.outputs import (
    TokenEvent,
    TokenUsage,
    UsageEvent,
    LLMDoneEvent,
    LLMStartEvent,
)


class SwarmMockChat(BaseLLMChat):
    """A deterministic chat that answers from the prompt it is given."""

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator:
        prompt = messages[-1].content if messages else ""
        reply = (
            f"[{config.model}] finished the task "
            f"({len(str(prompt).splitlines())} lines of context received)."
        )
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield TokenEvent(content=reply)
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=40, output=12),
            cost_usd=0.001,
        )
        yield LLMDoneEvent(content=reply, has_tool_calls=False)


@tool
def bash(cmd: str) -> str:
    """Stub bash — real members get the host's actual tools."""
    return "ok"


@tool
def web_search(query: str) -> str:
    """Stub web_search."""
    return "ok"


def _tool(plugin: SwarmPlugin, name: str):
    return next(t for t in plugin.get_tools() if t.name == name)


async def main() -> None:
    print("=" * 64)
    print("🐝 Agents Swarm example (issue #232) — mock LLM, no cost")
    print("=" * 64)

    # 1. Build + configure the plugin (opt-in config keys).
    plugin = SwarmPlugin()
    plugin.configure(
        {
            "max_agents": 5,
            "default_topology": "mesh",
            "max_tokens_per_agent": 4096,
            "max_tokens_total": 32768,
        }
    )
    plugin.initialize()

    # Host runtime bindings the swarm tools read from the tool context
    # (the CLI injects the same keys for its own sub-agent tools).
    chat = SwarmMockChat()
    ctx = AgentContext()
    ctx.extra["chat"] = chat
    ctx.extra["available_tools"] = {"bash": bash, "web_search": web_search}
    ctx.extra["default_model"] = "example-model"
    ctx.extra["middlewares"] = []
    ctx.extra["max_iterations"] = 4

    # 2. Create a 3-role research swarm (mesh: parallel + shared blackboard).
    created = await _tool(plugin, "swarm_create").handler(
        {
            "agents": [
                {
                    "name": "explorer",
                    "system_prompt": "Map the code and gather raw findings.",
                    "tools_allowlist": ["bash"],
                },
                {
                    "name": "reviewer",
                    "system_prompt": "Critique the explorer's findings.",
                    "tools_allowlist": ["bash", "web_search"],
                },
                {
                    "name": "writer",
                    "system_prompt": "Synthesize a single report.",
                    "tools_allowlist": [],  # reasons only over the blackboard
                },
            ],
            "topology": "mesh",
        },
        ctx,
    )
    print("\n📦 created swarm:")
    for agent in created["agents"]:
        print(f"   • {agent['name']}: tools={agent['tools']}")

    # 3. Route a message to one member (delivered on its next run).
    sent = await _tool(plugin, "swarm_message").handler(
        {"sender": "orchestrator", "recipient": "explorer", "content": "focus on /src"},
        ctx,
    )
    print(
        f"\n✉️  message sent (seq={sent['seq']}), "
        f"pending for explorer: {sent['pending_for_recipient']}"
    )

    # 4. Assign the whole swarm a task (starts in parallel; non-blocking).
    started = await _tool(plugin, "swarm_assign").handler(
        {"task": "research the repo and report"}, ctx
    )
    print(f"\n🚀 assigned; started: {started['started']}")

    # 5. Inspect live status, then wait for and collect all results.
    status = await _tool(plugin, "swarm_status").handler({}, ctx)
    print(f"📊 status: topology={status['topology']} in_flight={status['in_flight']}")

    collected = await _tool(plugin, "swarm_collect").handler({}, ctx)
    print(
        f"\n🧾 collected {len(collected['results'])} results "
        f"(tokens used: {collected['tokens_total_used']}):"
    )
    for result in collected["results"]:
        print(f"   • [{result['agent']}] {result['status']}: {result['content']}")

    # 6. Tear down.
    dissolved = await _tool(plugin, "swarm_dissolve").handler({}, ctx)
    print(f"\n🧹 dissolved: {dissolved['dissolved']}")

    print("\n✨ example complete")


if __name__ == "__main__":
    asyncio.run(main())
