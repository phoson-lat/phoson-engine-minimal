"""Sending a running tool call to the background (phoson_agent.detach)."""

import asyncio
from typing import Any

import pytest

from phoson_agent import AgentEngine, tool
from phoson_llm.schemas import (
    Message,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_agent.detach import DETACHER_KEY, detach_requested
from phoson_llm.chats.base import BaseLLMChat

CONFIG = ModelConfig(model="fake")


class ScriptedChat(BaseLLMChat):
    """Replays one scripted event list per ``stream()`` call."""

    def __init__(self, turns: list[list[Any]]) -> None:
        self.turns = list(turns)

    async def stream(self, messages, config, tools=None):
        yield LLMStartEvent(model="fake", message_count=len(messages))
        for event in self.turns.pop(0):
            yield event


def calls_turn(*calls: tuple[str, str, dict[str, Any]]) -> list[Any]:
    events: list[Any] = [
        ToolCallEvent(index=i, tool_call_id=cid, tool_name=name, args=args)
        for i, (cid, name, args) in enumerate(calls)
    ]
    events.append(LLMDoneEvent(content="", has_tool_calls=True))
    return events


def answer(text: str = "done") -> list[Any]:
    return [TokenEvent(content=text), LLMDoneEvent(content=text)]


async def collect(engine: AgentEngine) -> list[Any]:
    return [
        e async for e in engine.stream([Message(role="user", content="go")], CONFIG)
    ]


class _Detacher:
    """Detaches every tracked call as soon as it starts (when armed)."""

    def __init__(self, armed: bool = True) -> None:
        self.armed = armed
        self.tracked: list[Any] = []
        self.untracked: list[Any] = []
        self.tasks: list[asyncio.Task] = []

    def track(self, call, task):
        self.tracked.append(call)
        self.tasks.append(task)
        future = asyncio.get_running_loop().create_future()
        if self.armed:
            asyncio.get_running_loop().call_later(
                0.01, lambda: future.done() or future.set_result("moved to #1")
            )
        return future

    def untrack(self, call):
        self.untracked.append(call)


def _results(events) -> list[str]:
    history = events[-1].result.history
    return [
        b.result
        for m in history
        if not isinstance(m.content, str)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]


@pytest.mark.asyncio
async def test_detached_call_returns_placeholder_and_keeps_running() -> None:
    finished = asyncio.Event()
    saw_event: list[bool] = []

    @tool(detachable=True)
    async def slow() -> str:
        """Slow."""
        event = detach_requested()
        await asyncio.sleep(0.05)
        saw_event.append(event is not None and event.is_set())
        finished.set()
        return "real result"

    detacher = _Detacher()
    engine = AgentEngine(
        chat=ScriptedChat([calls_turn(("c1", "slow", {})), answer()]), tools=[slow]
    )
    engine.context.extra[DETACHER_KEY] = detacher

    events = await collect(engine)

    assert _results(events) == ["moved to #1"]
    assert not finished.is_set()  # the turn did not wait for it
    assert await detacher.tasks[0] == ("real result", None, False, None)
    assert saw_event == [True]  # the handler knew it was detached
    assert detacher.untracked == []  # the host owns it now


@pytest.mark.asyncio
async def test_call_finishing_first_is_a_normal_result() -> None:
    @tool(detachable=True)
    async def quick() -> str:
        """Quick."""
        return "fast"

    detacher = _Detacher(armed=False)
    engine = AgentEngine(
        chat=ScriptedChat([calls_turn(("c1", "quick", {})), answer()]), tools=[quick]
    )
    engine.context.extra[DETACHER_KEY] = detacher
    events = await collect(engine)
    assert _results(events) == ["fast"]
    assert len(detacher.untracked) == 1


@pytest.mark.asyncio
async def test_non_detachable_tools_and_no_detacher_run_inline() -> None:
    @tool
    async def plain() -> str:
        """Plain."""
        assert detach_requested() is None
        return "plain"

    detacher = _Detacher()
    engine = AgentEngine(
        chat=ScriptedChat([calls_turn(("c1", "plain", {})), answer()]), tools=[plain]
    )
    engine.context.extra[DETACHER_KEY] = detacher
    assert _results(await collect(engine)) == ["plain"]
    assert detacher.tracked == []

    @tool(detachable=True)
    async def alone() -> str:
        """Alone."""
        return "inline"

    engine = AgentEngine(
        chat=ScriptedChat([calls_turn(("c1", "alone", {})), answer()]), tools=[alone]
    )
    assert _results(await collect(engine)) == ["inline"]


@pytest.mark.asyncio
async def test_cancelling_the_turn_cancels_a_foreground_call() -> None:
    cancelled = asyncio.Event()

    @tool(detachable=True)
    async def hang() -> str:
        """Hang."""
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    detacher = _Detacher(armed=False)
    engine = AgentEngine(
        chat=ScriptedChat([calls_turn(("c1", "hang", {})), answer()]), tools=[hang]
    )
    engine.context.extra[DETACHER_KEY] = detacher
    run = asyncio.create_task(collect(engine))
    await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    await asyncio.sleep(0)
    assert cancelled.is_set()
    assert len(detacher.untracked) == 1


def test_tool_decorator_marks_detachable() -> None:
    @tool(detachable=True)
    def a() -> str:
        """A."""
        return ""

    @tool
    def b() -> str:
        """B."""
        return ""

    assert a.detachable is True and b.detachable is False
