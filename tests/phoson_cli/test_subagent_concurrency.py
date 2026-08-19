"""Unit tests for sub-agent concurrency limit and per-task timeout."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from phoson_llm.schemas import Message, LLMEvent, ModelConfig, LLMDoneEvent
from phoson_llm.chats.base import BaseLLMChat
from phoson_cli.tools.subagent import agent, agents


class ConcurrencyChat(BaseLLMChat):
    """Tracks how many sub-agent streams are active at once.

    ``stats`` is a plain dict stored as an instance attribute, so the
    shallow copy done by ``_clone_chat`` keeps a reference to the same
    shared counter.
    """

    def __init__(self, stats: dict) -> None:
        self.stats = stats

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.stats["active"] = self.stats.get("active", 0) + 1
        self.stats["peak"] = max(self.stats.get("peak", 0), self.stats["active"])
        await asyncio.sleep(0.02)
        self.stats["active"] -= 1
        user_text = messages[-1].content
        assert isinstance(user_text, str)
        yield LLMDoneEvent(content=f"done:{user_text}", has_tool_calls=False)


class SlowChat(BaseLLMChat):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        await asyncio.sleep(self.delay)
        user_text = messages[-1].content
        assert isinstance(user_text, str)
        yield LLMDoneEvent(content=f"done:{user_text}", has_tool_calls=False)


def _context(**overrides):
    base = {
        "chat": None,
        "available_tools": {"agent": agent, "agents": agents},
        "default_model": "fake-model",
        "max_iterations": 2,
        "safe_mode": False,
        "subagent_max_parallel": 4,
        "subagent_timeout_seconds": 300.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_agents_respects_parallel_limit() -> None:
    stats: dict = {}
    tasks = [f"task-{i}" for i in range(8)]

    result = await agents.handler(
        {"tasks": tasks},
        _context(chat=ConcurrencyChat(stats), subagent_max_parallel=3),
    )

    # All 8 tasks complete...
    for i in range(8):
        assert f"done:task-{i}" in result
    # ...but never more than 3 at a time.
    assert stats["peak"] <= 3
    # And the limit was actually exercised (not serialized to 1).
    assert stats["peak"] >= 2


@pytest.mark.asyncio
async def test_agents_parallel_limit_of_one_serializes() -> None:
    stats: dict = {}

    await agents.handler(
        {"tasks": ["a", "b", "c"]},
        _context(chat=ConcurrencyChat(stats), subagent_max_parallel=1),
    )

    assert stats["peak"] == 1


@pytest.mark.asyncio
async def test_agents_timeout_reports_error_block() -> None:
    result = await agents.handler(
        {"tasks": ["slow"]},
        _context(chat=SlowChat(0.3), subagent_timeout_seconds=0.05),
    )

    assert "timeout after 0.05s" in result


@pytest.mark.asyncio
async def test_agent_single_tool_timeout() -> None:
    result = await agent.handler(
        {"task": "slow"},
        _context(chat=SlowChat(0.3), subagent_timeout_seconds=0.05),
    )

    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_agent_single_tool_succeeds_before_timeout() -> None:
    result = await agent.handler(
        {"task": "fast"},
        _context(chat=SlowChat(0.01), subagent_timeout_seconds=5.0),
    )

    assert result == "done:fast"
