import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from phoson_agent import AgentToolDoneEvent, AgentToolStartEvent
from phoson_agent.tool import tool
from phoson_llm.schemas import Message, LLMEvent, ModelConfig, LLMDoneEvent
from phoson_cli.renderer import Renderer
from phoson_llm.chats.base import BaseLLMChat
from phoson_cli.tools.subagent import agent, agents
from phoson_cli.tools.subagent_panel import render_subagent_panel_frame


class FakeSubagentChat(BaseLLMChat):
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        user_text = messages[-1].content
        assert isinstance(user_text, str)
        yield LLMDoneEvent(content=f"summary:{user_text}", has_tool_calls=False)


class DelayedSubagentChat(BaseLLMChat):
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        user_text = messages[-1].content
        assert isinstance(user_text, str)
        await asyncio.sleep(0.05 if "PROJECT" in user_text else 0.01)
        yield LLMDoneEvent(content=f"summary:{user_text}", has_tool_calls=False)


class SingleStreamChat(BaseLLMChat):
    def __init__(self) -> None:
        self._active = False

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        if self._active:
            raise RuntimeError("shared chat instance used concurrently")

        self._active = True
        try:
            user_text = messages[-1].content
            assert isinstance(user_text, str)
            await asyncio.sleep(0.01)
            yield LLMDoneEvent(content=f"summary:{user_text}", has_tool_calls=False)
        finally:
            self._active = False


@pytest.mark.asyncio
async def test_agent_tool_awaits_subengine_run() -> None:
    result = await agent.handler(
        {"task": "read PROJECT.md"},
        {
            "chat": FakeSubagentChat(),
            "available_tools": {"agent": agent, "agents": agents},
            "default_model": "fake-demo-model",
            "max_iterations": 2,
            "safe_mode": False,
        },
    )

    assert result == "summary:read PROJECT.md"


@pytest.mark.asyncio
async def test_agents_tool_awaits_each_subengine_run() -> None:
    result = await agents.handler(
        {"tasks": ["read PROJECT.md", "read README.md"]},
        {
            "chat": FakeSubagentChat(),
            "available_tools": {"agent": agent, "agents": agents},
            "default_model": "fake-demo-model",
            "max_iterations": 2,
            "safe_mode": False,
        },
    )

    assert "=== Agent 0: read PROJECT.md ===\nsummary:read PROJECT.md" in result
    assert "=== Agent 1: read README.md ===\nsummary:read README.md" in result


@pytest.mark.asyncio
async def test_agents_tool_runs_subagents_concurrently_and_keeps_output_order() -> None:
    result = await agents.handler(
        {"tasks": ["read PROJECT.md", "read README.md"]},
        {
            "chat": DelayedSubagentChat(),
            "available_tools": {"agent": agent, "agents": agents},
            "default_model": "fake-demo-model",
            "max_iterations": 2,
            "safe_mode": False,
        },
    )

    assert result.index("=== Agent 0: read PROJECT.md ===") < result.index(
        "=== Agent 1: read README.md ==="
    )


@pytest.mark.asyncio
async def test_agents_tool_uses_isolated_chat_instances_for_parallel_runs() -> None:
    result = await agents.handler(
        {"tasks": ["read PROJECT.md", "read README.md"]},
        {
            "chat": SingleStreamChat(),
            "available_tools": {"agent": agent, "agents": agents},
            "default_model": "fake-demo-model",
            "max_iterations": 2,
            "safe_mode": False,
        },
    )

    assert "Error:" not in result


def test_renderer_shows_subagent_spawn_states() -> None:
    renderer = Renderer()

    with renderer.console.capture() as capture:
        renderer._on_tool_start(
            AgentToolStartEvent(tool_name="agent", args={"task": "x"}, label="subagent")
        )
        renderer._on_tool_done(
            AgentToolDoneEvent(tool_name="agents", duration_ms=12, label="subagents")
        )

    output = capture.get()
    assert "spawning subagent" in output
    assert "spawned subagents" in output


def test_renderer_shows_subagent_panel_on_start() -> None:
    renderer = Renderer()

    with renderer.console.capture() as capture:
        renderer._on_tool_start(
            AgentToolStartEvent(
                tool_name="agents",
                args={"tasks": ["Read README.md", "Read PROJECT.md"]},
                label="subagents",
            )
        )

    output = capture.get()
    assert "spawning subagents" in output
    assert renderer._subagent_live is not None
    renderer.stop_subagent_waiting()


def test_subagent_panel_frame_changes_spinner() -> None:
    first = render_subagent_panel_frame(["Read README.md"], 0)
    second = render_subagent_panel_frame(["Read README.md"], 1)

    assert first.columns[1]._cells[0] != second.columns[1]._cells[0]


def test_renderer_shows_subagent_summary_on_done() -> None:
    renderer = Renderer()
    result = (
        "=== Agent 0: Read README.md ===\n"
        "done\n"
        "--- METRICS: 200ms | 124in/89out | $0.00100 ---\n\n"
        "=== Agent 1: Read PROJECT.md ===\n"
        "done\n"
        "--- METRICS: 300ms | 234in/156out | $0.00200 ---\n\n"
        "=== SUMMARY ===\n"
        "Total: 2 agents | 500ms | 358in/245out | $0.00300"
    )

    with renderer.console.capture() as capture:
        renderer._on_tool_done(
            AgentToolDoneEvent(
                tool_name="agents",
                duration_ms=500,
                label="subagents",
                result=result,
            )
        )

    output = capture.get()
    assert "2/2 parallel agents completed" in output
    assert "Read README.md" in output
    assert "Read PROJECT.md" in output
    assert "358in / 245out" in output


def test_subagent_debug_logging_is_emitted_when_enabled(monkeypatch, caplog) -> None:
    monkeypatch.setenv("PHOSON_SUBAGENT_DEBUG", "1")
    caplog.set_level(logging.DEBUG, logger="phoson_cli.subagent")

    from phoson_cli.tools.subagent import _log_debug

    _log_debug("debug event", idx=1, task_preview="read README.md")

    assert "debug event" in caplog.text
    assert "idx=1" in caplog.text


def test_agents_tool_schema_uses_array_for_tasks() -> None:
    assert agents.parameters["properties"]["tasks"]["type"] == "array"
    assert agents.parameters["properties"]["tasks"]["items"]["type"] == "string"


def test_tool_decorator_builds_array_schema_for_list_of_strings() -> None:
    @tool
    def example(items: list[str]) -> str:
        return ",".join(items)

    assert example.parameters == {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["items"],
    }
