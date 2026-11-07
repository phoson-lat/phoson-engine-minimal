"""Engine robustness regressions (engine review, 2026-11).

1. Exceptions from middleware hooks / the LLM stream never escape ``stream()``
   and never leave a ``tool_use`` without its ``tool_result``.
2. A retryable failure *after* visible output re-runs the turn once
   (``AgentRetryEvent``) instead of failing the run.
3. Consecutive ``parallel_safe`` tool calls run concurrently, in-order commit.
4. The caller's ``tools`` / ``middlewares`` lists are never mutated.
5. Image tool results are appended after *all* tool results of the turn.
"""

import asyncio
import threading
from typing import Any
from collections.abc import AsyncIterator

import pytest

from phoson_agent import (
    Plugin,
    AgentTool,
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentMiddleware,
    AgentRetryEvent,
    ImageToolResult,
    AgentToolDoneEvent,
    tool,
)
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_llm.chats.base import BaseLLMChat

CONFIG = ModelConfig(model="fake")


class ScriptedChat(BaseLLMChat):
    """Replays one scripted event list per ``stream()`` call."""

    def __init__(self, turns: list[list[LLMEvent]]) -> None:
        self.turns = list(turns)
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: Any = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        yield LLMStartEvent(model="fake", message_count=len(messages))
        for event in self.turns.pop(0):
            yield event


def calls_turn(*calls: tuple[str, str, dict[str, Any]]) -> list[LLMEvent]:
    events: list[LLMEvent] = [
        ToolCallEvent(index=i, tool_call_id=cid, tool_name=name, args=args)
        for i, (cid, name, args) in enumerate(calls)
    ]
    events.append(LLMDoneEvent(content="", has_tool_calls=True))
    return events


def answer(text: str = "done") -> list[LLMEvent]:
    return [TokenEvent(content=text), LLMDoneEvent(content=text)]


def assert_paired(history: list[Message]) -> None:
    """Every tool_use has exactly one tool_result, before the next assistant."""
    pending: set[str] = set()
    for msg in history:
        if msg.role == "assistant":
            assert not pending, f"unanswered tool_use: {pending}"
        if isinstance(msg.content, str):
            continue
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                pending.add(block.tool_call_id)
            elif isinstance(block, ToolResultBlock):
                pending.remove(block.tool_call_id)
    assert not pending, f"unanswered tool_use: {pending}"


async def collect(engine: AgentEngine) -> list[Any]:
    return [
        e async for e in engine.stream([Message(role="user", content="go")], CONFIG)
    ]


@tool
def echo(s: str) -> str:
    """Echo."""
    return s


# ── 1. Exceptions never escape / never orphan a tool_use ───────────────────


async def test_before_tool_exception_becomes_error_result() -> None:
    class Broken(AgentMiddleware):
        async def on_before_tool(self, call: ToolCallEvent) -> ToolCallEvent:
            raise RuntimeError("bug in hook")

    chat = ScriptedChat([calls_turn(("c1", "echo", {"s": "a"})), answer()])
    engine = AgentEngine(chat=chat, tools=[echo], middlewares=[Broken()])

    events = await collect(engine)

    done = [e for e in events if isinstance(e, AgentToolDoneEvent)]
    assert done[0].error == "middleware_error"
    assert "RuntimeError: bug in hook" in done[0].result
    assert isinstance(events[-1], AgentDoneEvent)
    assert_paired(events[-1].result.history)


async def test_on_agent_event_exception_is_ignored() -> None:
    class Noisy(AgentMiddleware):
        async def on_agent_event(self, event: Any) -> None:
            raise ValueError("observer bug")

    chat = ScriptedChat([calls_turn(("c1", "echo", {"s": "a"})), answer()])
    engine = AgentEngine(chat=chat, tools=[echo], middlewares=[Noisy()])

    events = await collect(engine)

    assert isinstance(events[-1], AgentDoneEvent)
    assert_paired(events[-1].result.history)


async def test_llm_stream_exception_becomes_error_event() -> None:
    class Exploding(BaseLLMChat):
        async def stream(self, messages, config, tools=None):  # type: ignore[override]
            yield LLMStartEvent(model="fake", message_count=1)
            raise KeyError("adapter bug")

    engine = AgentEngine(chat=Exploding())

    events = await collect(engine)

    assert isinstance(events[-1], AgentErrorEvent)
    assert events[-1].code == "llm_exception"
    assert "KeyError" in events[-1].message


async def test_before_llm_exception_becomes_error_event() -> None:
    class Broken(AgentMiddleware):
        async def on_before_llm(self, messages, config):  # type: ignore[override]
            raise RuntimeError("compaction bug")

    engine = AgentEngine(chat=ScriptedChat([answer()]), middlewares=[Broken()])

    events = await collect(engine)

    assert isinstance(events[-1], AgentErrorEvent)
    assert events[-1].code == "middleware_error"


async def test_consumer_closing_stream_backfills_results() -> None:
    @tool
    async def slow() -> str:
        """Slow."""
        await asyncio.sleep(10)
        return "never"

    chat = ScriptedChat(
        [
            calls_turn(
                ("c1", "echo", {"s": "a"}),
                ("c2", "slow", {}),
                ("c3", "echo", {"s": "b"}),
            )
        ]
    )
    engine = AgentEngine(chat=chat, tools=[echo, slow])

    task = asyncio.ensure_future(collect(engine))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert_paired(engine.get_partial_history())


# ── 2. Mid-stream retry ─────────────────────────────────────────────────────


def broken_turn(retryable: bool = True) -> list[LLMEvent]:
    return [
        TokenEvent(content="partial ans"),
        ErrorEvent(message="overloaded", code="overloaded", retryable=retryable),
    ]


async def test_mid_stream_retryable_error_retries_turn() -> None:
    chat = ScriptedChat([broken_turn(), answer("full answer")])
    engine = AgentEngine(chat=chat, stream_retry_delay=0)

    events = await collect(engine)

    retries = [e for e in events if isinstance(e, AgentRetryEvent)]
    assert len(retries) == 1
    assert retries[0].attempt == 1 and retries[0].code == "overloaded"
    assert isinstance(events[-1], AgentDoneEvent)
    result = events[-1].result
    assert result.final_content == "full answer"
    texts = [m.content for m in result.history if m.role == "assistant"]
    assert all("partial" not in str(t) for t in texts)
    assert chat.calls == 2


async def test_retry_budget_is_respected() -> None:
    chat = ScriptedChat([broken_turn(), broken_turn()])
    engine = AgentEngine(chat=chat, stream_retry_delay=0, max_stream_retries=1)

    events = await collect(engine)

    assert sum(isinstance(e, AgentRetryEvent) for e in events) == 1
    assert isinstance(events[-1], AgentErrorEvent)


@pytest.mark.parametrize(
    "turn,kwargs",
    [
        (broken_turn(retryable=False), {}),
        (broken_turn(), {"max_stream_retries": 0}),
        # Pre-output failures belong to RetryingChat, not the loop.
        ([ErrorEvent(message="rate", code="rate_limit", retryable=True)], {}),
    ],
)
async def test_no_retry_cases(turn: list[LLMEvent], kwargs: dict[str, Any]) -> None:
    engine = AgentEngine(chat=ScriptedChat([turn]), stream_retry_delay=0, **kwargs)

    events = await collect(engine)

    assert not any(isinstance(e, AgentRetryEvent) for e in events)
    assert isinstance(events[-1], AgentErrorEvent)


# ── 3. Parallel-safe tools ──────────────────────────────────────────────────


async def test_parallel_safe_async_tools_run_concurrently() -> None:
    running = 0
    peak = 0

    @tool(parallel_safe=True)
    async def look(s: str) -> str:
        """Look."""
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return s

    chat = ScriptedChat(
        [
            calls_turn(("c1", "look", {"s": "1"}), ("c2", "look", {"s": "2"})),
            answer(),
        ]
    )
    engine = AgentEngine(chat=chat, tools=[look])

    events = await collect(engine)

    assert peak == 2
    history = events[-1].result.history
    results = [
        b.result
        for m in history
        if not isinstance(m.content, str)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert results == ["1", "2"]
    assert_paired(history)


async def test_parallel_safe_sync_tools_run_in_threads() -> None:
    barrier = threading.Barrier(2, timeout=2)

    @tool(parallel_safe=True)
    def meet() -> str:
        """Both calls must be in flight at once or the barrier breaks."""
        barrier.wait()
        return "met"

    chat = ScriptedChat([calls_turn(("c1", "meet", {}), ("c2", "meet", {})), answer()])
    engine = AgentEngine(chat=chat, tools=[meet])

    events = await collect(engine)

    done = [e for e in events if isinstance(e, AgentToolDoneEvent)]
    assert [d.error for d in done] == [None, None]


async def test_unsafe_tool_splits_batches_and_hooks_stay_sequential() -> None:
    order: list[str] = []
    in_hook = 0

    class Track(AgentMiddleware):
        async def on_before_tool(self, call: ToolCallEvent) -> ToolCallEvent:
            nonlocal in_hook
            in_hook += 1
            assert in_hook == 1, "before_tool hooks overlapped"
            await asyncio.sleep(0.005)
            in_hook -= 1
            return call

    @tool(parallel_safe=True)
    async def read(s: str) -> str:
        """Read."""
        order.append(f"read{s}")
        return s

    @tool
    async def write(s: str) -> str:
        """Write."""
        order.append(f"write{s}")
        return s

    chat = ScriptedChat(
        [
            calls_turn(
                ("c1", "read", {"s": "1"}),
                ("c2", "read", {"s": "2"}),
                ("c3", "write", {"s": "3"}),
                ("c4", "read", {"s": "4"}),
            ),
            answer(),
        ]
    )
    engine = AgentEngine(chat=chat, tools=[read, write], middlewares=[Track()])

    events = await collect(engine)

    # The write is a barrier: both earlier reads finish before it, the
    # later read starts after it.
    assert order.index("write3") == 2
    assert order[3] == "read4"
    assert_paired(events[-1].result.history)


# ── 4. Caller lists are not mutated ─────────────────────────────────────────


async def test_plugins_do_not_mutate_caller_lists() -> None:
    class Extra(Plugin):
        @property
        def name(self) -> str:
            return "extra"

        def get_tools(self) -> list[AgentTool]:
            @tool
            def plugin_tool() -> str:
                """Plugin tool."""
                return "x"

            return [plugin_tool]

        def get_middlewares(self) -> list[AgentMiddleware]:
            return [AgentMiddleware()]

    tools = [echo]
    middlewares: list[AgentMiddleware] = []
    first = AgentEngine(
        chat=ScriptedChat([]), tools=tools, middlewares=middlewares, plugins=[Extra()]
    )
    second = AgentEngine(
        chat=ScriptedChat([]), tools=tools, middlewares=middlewares, plugins=[Extra()]
    )

    assert tools == [echo]
    assert middlewares == []
    assert [t.name for t in first.tools] == ["echo", "plugin_tool"]
    assert [t.name for t in second.tools] == ["echo", "plugin_tool"]


# ── 5. Images go after every tool result ────────────────────────────────────


async def test_image_results_follow_all_tool_results() -> None:
    image = ImageBlock(source="data:image/png;base64,AAAA")

    @tool
    def snap() -> ImageToolResult:
        """Snap."""
        return ImageToolResult(text="snapped", image=image)

    chat = ScriptedChat(
        [calls_turn(("c1", "snap", {}), ("c2", "echo", {"s": "b"})), answer()]
    )
    engine = AgentEngine(chat=chat, tools=[snap, echo])

    events = await collect(engine)

    history = events[-1].result.history
    kinds = [
        type(m.content[0]).__name__
        for m in history
        if m.role == "user" and not isinstance(m.content, str)
    ]
    assert kinds == ["ToolResultBlock", "ToolResultBlock", "ImageBlock"]
