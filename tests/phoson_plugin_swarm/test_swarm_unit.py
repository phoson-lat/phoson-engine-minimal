"""Unit tests for the swarm plugin (issue #232) — mock LLM, no real calls."""

from typing import Any
from collections.abc import AsyncIterator

import pytest

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine  # noqa: F401  (import sanity)
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.models import AgentTool
from phoson_plugin_swarm import (
    AgentRole,
    SwarmError,
    SharedState,
    SwarmPlugin,
    TokenBudget,
)
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.schemas.outputs import (
    TokenEvent,
    TokenUsage,
    UsageEvent,
    LLMDoneEvent,
    LLMStartEvent,
)
from phoson_plugin_swarm._orchestrator import build_runtime_from_roles


class SwarmMockChat(BaseLLMChat):
    """A deterministic chat that records the tools it is handed."""

    def __init__(self, content="report", input_tokens=10, output_tokens=5) -> None:
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = 0
        self.seen_tool_names: list[list[str]] = []

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[Any] | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        self.seen_tool_names.append(sorted(t.name for t in (tools or [])))
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield TokenEvent(content=self.content)
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=self.input_tokens, output=self.output_tokens),
            cost_usd=0.01,
        )
        yield LLMDoneEvent(content=self.content, has_tool_calls=False)


@tool
def bash(cmd: str) -> str:
    """fake bash."""
    return "ok"


@tool
def web_search(query: str) -> str:
    """fake web search."""
    return "ok"


@tool
def agent(task: str) -> str:
    """fake delegation tool (must be stripped from members)."""
    return "ok"


def _make_tools() -> dict[str, AgentTool]:
    return {"bash": bash, "web_search": web_search, "agent": agent}


def _ctx(chat: BaseLLMChat, available: dict[str, AgentTool]) -> AgentContext:
    ctx = AgentContext()
    ctx.extra["chat"] = chat
    ctx.extra["available_tools"] = available
    ctx.extra["default_model"] = "test-model"
    ctx.extra["middlewares"] = []
    ctx.extra["max_iterations"] = 4
    ctx.extra["safe_mode"] = False
    return ctx


def _tool(plugin: SwarmPlugin, name: str) -> AgentTool:
    return next(t for t in plugin.get_tools() if t.name == name)


# ── Pure unit tests (no event loop needed) ──────────────────────────────────


def test_agent_role_validation() -> None:
    role = AgentRole.from_dict({"name": "x", "system_prompt": "s"})
    assert role.name == "x" and role.model is None
    with pytest.raises(SwarmError):
        AgentRole.from_dict({"name": "", "system_prompt": "s"})
    with pytest.raises(SwarmError):
        AgentRole.from_dict({"name": "x"})
    with pytest.raises(SwarmError):
        AgentRole.from_dict({"name": "x", "system_prompt": "s", "max_tokens": -1})


def test_token_budget() -> None:
    budget = TokenBudget(max_per_agent=100, max_total=1000)
    budget.consume("a", 60)
    assert budget.used("a") == 60
    assert not budget.allow("a", 50)  # 60 + 50 > 100
    # total cap
    tb = TokenBudget(max_total=10)
    tb.consume("a", 8)
    assert not tb.allow("b", 3)  # 8 + 3 = 11 > 10


def test_blackboard_routing() -> None:
    state = SharedState()
    state.send("orchestrator", "explorer", "hi", topic="t")
    assert state.pending_count("explorer") == 1
    assert len(state.inbox("explorer")) == 1
    assert state.pending_count("writer") == 0
    # broadcast
    state.send("orchestrator", "*", "bcast")
    assert state.pending_count("writer") == 1
    with pytest.raises(SwarmError):
        state.send("o", "x", "   ")


def test_allowlist_enforced_on_resolved_tools() -> None:
    runtime = build_runtime_from_roles(
        [
            {"name": "explorer", "system_prompt": "s", "tools_allowlist": ["bash"]},
            {"name": "writer", "system_prompt": "s"},
        ],
        topology="star",
        max_tokens_per_agent=1000,
        max_tokens_total=10000,
    )
    runtime._available_tools = _make_tools()
    explorer = next(i for i in runtime.instances if i.name == "explorer")
    writer = next(i for i in runtime.instances if i.name == "writer")
    # explorer is restricted to bash only
    assert [t.name for t in runtime._resolve_tools(explorer.role)] == ["bash"]
    # writer gets everything except the reserved delegation tool
    writer_tools = {t.name for t in runtime._resolve_tools(writer.role)}
    assert "agent" not in writer_tools
    assert {"bash", "web_search"} <= writer_tools


# ── Tool-handler tests (single event loop each) ─────────────────────────────


async def test_swarm_create_and_status() -> None:
    plugin = SwarmPlugin()
    plugin.configure({})
    plugin.initialize()
    chat = SwarmMockChat()
    ctx = _ctx(chat, _make_tools())

    created = await _tool(plugin, "swarm_create").handler(
        {
            "agents": [
                {"name": "explorer", "system_prompt": "explore"},
                {"name": "writer", "system_prompt": "write"},
            ],
            "topology": "star",
        },
        ctx,
    )
    assert created["created"] is True
    assert [a["name"] for a in created["agents"]] == ["explorer", "writer"]

    status = await _tool(plugin, "swarm_status").handler({}, ctx)
    assert status["topology"] == "star"
    assert status["agents"] and status["in_flight"] == []


async def test_swarm_message_routing_and_collect() -> None:
    plugin = SwarmPlugin()
    plugin.configure({})
    plugin.initialize()
    chat = SwarmMockChat(content="found 3 sources")
    ctx = _ctx(chat, _make_tools())

    await _tool(plugin, "swarm_create").handler(
        {
            "agents": [
                {"name": "explorer", "system_prompt": "explore the web"},
                {"name": "writer", "system_prompt": "write a report"},
            ],
            "topology": "mesh",
        },
        ctx,
    )
    # message routing
    sent = await _tool(plugin, "swarm_message").handler(
        {"sender": "orchestrator", "recipient": "explorer", "content": "check docs"},
        ctx,
    )
    assert sent["sent"] is True
    assert sent["pending_for_recipient"] == 1
    with pytest.raises(SwarmError):
        await _tool(plugin, "swarm_message").handler(
            {"sender": "o", "recipient": "nobody", "content": "x"}, ctx
        )

    # assign to the whole swarm, then collect
    started = await _tool(plugin, "swarm_assign").handler({"task": "research"}, ctx)
    assert set(started["started"]) == {"explorer", "writer"}

    collected = await _tool(plugin, "swarm_collect").handler({}, ctx)
    results = {r["agent"]: r for r in collected["results"]}
    assert set(results) == {"explorer", "writer"}
    for r in results.values():
        assert r["status"] == "done"
        assert r["content"] == "found 3 sources"
    assert collected["tokens_total_used"] > 0
    assert len(collected["results"]) == 2


async def test_swarm_pipeline_collect() -> None:
    plugin = SwarmPlugin()
    plugin.configure({})
    plugin.initialize()
    chat = SwarmMockChat(content="stage-out")
    ctx = _ctx(chat, _make_tools())

    await _tool(plugin, "swarm_create").handler(
        {
            "agents": [
                {"name": "parser", "system_prompt": "parse"},
                {"name": "writer", "system_prompt": "write"},
            ],
            "topology": "pipeline",
        },
        ctx,
    )
    await _tool(plugin, "swarm_assign").handler({"task": "migrate"}, ctx)
    collected = await _tool(plugin, "swarm_collect").handler({}, ctx)
    results = {r["agent"]: r for r in collected["results"]}
    assert set(results) == {"parser", "writer"}
    assert all(r["status"] == "done" for r in results.values())
    # the second stage was seeded with the first stage's output on the board
    assert plugin.runtime.state.read("stage:parser") == "stage-out"


async def test_token_budget_stops_gracefully() -> None:
    plugin = SwarmPlugin()
    plugin.configure({"max_tokens_per_agent": 10, "max_tokens_total": 1000})
    plugin.initialize()
    # each LLM step uses 10 + 5 = 15 tokens > the per-agent cap of 10
    chat = SwarmMockChat(content="partial", input_tokens=10, output_tokens=5)
    ctx = _ctx(chat, _make_tools())

    await _tool(plugin, "swarm_create").handler(
        {
            "agents": [{"name": "explorer", "system_prompt": "explore"}],
            "topology": "star",
        },
        ctx,
    )
    await _tool(plugin, "swarm_assign").handler({"task": "go"}, ctx)
    collected = await _tool(plugin, "swarm_collect").handler({}, ctx)
    results = {r["agent"]: r for r in collected["results"]}
    assert results["explorer"]["status"] == "budget_exhausted"
    assert results["explorer"]["error"]


async def test_swarm_dissolve() -> None:
    plugin = SwarmPlugin()
    plugin.configure({})
    plugin.initialize()
    chat = SwarmMockChat()
    ctx = _ctx(chat, _make_tools())

    await _tool(plugin, "swarm_create").handler(
        {"agents": [{"name": "a", "system_prompt": "s"}], "topology": "star"}, ctx
    )
    dissolved = await _tool(plugin, "swarm_dissolve").handler({}, ctx)
    assert dissolved["dissolved"] is True
    with pytest.raises(SwarmError):
        await _tool(plugin, "swarm_assign").handler({"task": "x"}, ctx)
