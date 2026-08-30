"""Unit tests for the tool-call composing feedback (I-128).

Covers:
1. ``_ComposingTracker`` leading-edge throttle semantics.
2. ``AgentLoop._consume_llm_stream`` demoting ``ToolCallDeltaEvent``s into
   (throttled) ``AgentToolComposingEvent``s.
3. Engine-level: composing events reach the consumer *before*
   ``AgentToolStartEvent``, never fire for text/reasoning-only streams,
   and survive an error mid-composing.
"""

from typing import Any
from collections.abc import AsyncIterator

import pytest

from phoson_agent import AgentToolComposingEvent
from phoson_agent._loop import _COMPOSING_THROTTLE_S as THROTTLE
from phoson_agent._loop import _ComposingTracker
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolCallDeltaEvent,
    ReasoningTokenEvent,
)
from phoson_agent.models import (
    AgentDoneEvent,
    AgentErrorEvent,
    AgentTokenEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent._internals import LLMStepOutcome
from phoson_agent._tool_runner import ToolRunner

CONFIG = ModelConfig(model="fake-model")
EMPTY_HISTORY: list[Message] = []


# ── 1. _ComposingTracker throttle semantics ──────────────────────────────


def _fake_clock(start: float = 1000.0):
    state = {"t": start}

    def now() -> float:
        return state["t"]

    def advance(delta: float) -> None:
        state["t"] += delta

    return now, advance


def test_tracker_first_args_chunk_always_emits() -> None:
    now, _ = _fake_clock()
    tracker = _ComposingTracker(now=now)
    assert tracker.should_emit("", '{"path": "s') is True
    tracker.record_emitted("")
    # Same instant, more args: throttled.
    assert tracker.should_emit("", 'rc/main.py"}') is False


def test_tracker_first_known_name_always_emits() -> None:
    now, _ = _fake_clock()
    tracker = _ComposingTracker(now=now)
    # Args fragment arrives before the name is known.
    assert tracker.should_emit("", '{"a":') is True
    tracker.record_emitted("")
    # Name becomes known on a later chunk: always emits, same instant.
    assert tracker.should_emit("write_file", "1}") is True
    tracker.record_emitted("write_file")
    # Subsequent args chunks are throttled again.
    assert tracker.should_emit("write_file", " more") is False


def test_tracker_heartbeat_after_throttle_window() -> None:
    now, advance = _fake_clock()
    tracker = _ComposingTracker(now=now)
    assert tracker.should_emit("", '{"a":') is True
    tracker.record_emitted("")
    assert tracker.should_emit("", "1,") is False
    advance(THROTTLE)
    assert tracker.should_emit("", "2,") is True
    tracker.record_emitted("")
    assert tracker.should_emit("", "3}") is False


def test_tracker_empty_delta_never_emits() -> None:
    now, _ = _fake_clock()
    tracker = _ComposingTracker(now=now)
    assert tracker.should_emit("", "") is False
    tracker.record_emitted("")
    assert tracker.should_emit("", "") is False


# ── 2. _consume_llm_stream demultiplexing ─────────────────────────────────


class _FakeClock:
    """Monotonic stand-in: tests advance it explicitly."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def _make_loop() -> Any:
    from phoson_agent import AgentContext

    runner = ToolRunner(
        tools_by_name={},
        context=AgentContext(),
        apply_before_tool=lambda call: _never(),
        apply_after_tool=lambda call, res, err: _never(),
        prepare_event=lambda event: _never(),
    )
    return _LoopOnly(runner)


async def _never() -> Any:
    raise AssertionError("should not be called")


class _LoopOnly:
    """AgentLoop without a real runner (only the stream is exercised)."""

    def __init__(self, runner: ToolRunner) -> None:
        from phoson_agent._loop import AgentLoop

        self.loop = AgentLoop(
            tool_runner=runner,
            prepare_event=lambda event: _prep(event),
            phoson_weight=1.0,
        )


async def _prep(event: Any) -> Any:
    return event


async def _consume(events: list[LLMEvent]) -> list[Any]:
    loop = _make_loop().loop

    async def llm_call(
        history: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        for event in events:
            yield event

    outcome = LLMStepOutcome()
    return [
        event
        async for event in loop._consume_llm_stream(
            llm_call=llm_call, history=EMPTY_HISTORY, config=CONFIG, outcome=outcome
        )
    ]


def _delta(index: int, tool_name: str, chunk: str) -> ToolCallDeltaEvent:
    return ToolCallDeltaEvent(index=index, tool_name=tool_name, args_chunk=chunk)


@pytest.mark.asyncio
async def test_consume_emits_composing_for_deltas(monkeypatch) -> None:
    from phoson_agent import _loop as loop_mod

    clock = _FakeClock()
    monkeypatch.setattr(loop_mod.time, "monotonic", clock)

    events = [
        _delta(0, "write_file", '{"path": "src/main.py", "content": "line1\n'),
        _delta(0, "write_file", 'line2\n"}'),
        ToolCallEvent(
            index=0,
            tool_call_id="call_1",
            tool_name="write_file",
            args={"path": "src/main.py", "content": "line1\nline2\n"},
        ),
        LLMDoneEvent(content="", has_tool_calls=True),
    ]
    loop = _make_loop().loop

    async def llm_call(
        history: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        for event in events:
            yield event
            # Space each event one full throttle window apart so both
            # composing emissions pass the leading-edge filter.
            clock.advance(THROTTLE)

    outcome = LLMStepOutcome()
    out = [
        event
        async for event in loop._consume_llm_stream(
            llm_call=llm_call, history=EMPTY_HISTORY, config=CONFIG, outcome=outcome
        )
    ]
    composing = [e for e in out if isinstance(e, AgentToolComposingEvent)]
    assert len(composing) == 2
    assert composing[0].tool_name == "write_file"
    assert composing[0].index == 0
    assert composing[0].tool_call_id == ""
    assert composing[0].args_chunk == '{"path": "src/main.py", "content": "line1\n'


@pytest.mark.asyncio
async def test_consume_text_and_reasoning_only_emit_no_composing() -> None:
    out = await _consume(
        [
            TokenEvent(content="hello "),
            ReasoningTokenEvent(content="hmm"),
            TokenEvent(content="world"),
            LLMDoneEvent(content="hello world", has_tool_calls=False),
        ]
    )
    kinds = [type(e) for e in out]
    assert AgentToolComposingEvent not in kinds
    assert kinds == [AgentTokenEvent, AgentReasoningEvent, AgentTokenEvent]


@pytest.mark.asyncio
async def test_consume_throttles_dense_delta_stream(monkeypatch) -> None:
    from phoson_agent import _loop as loop_mod

    clock = _FakeClock()
    monkeypatch.setattr(loop_mod.time, "monotonic", clock)

    events = [_delta(0, "bash", f"part{i} ") for i in range(50)]
    out = await _consume(events)
    composing = [e for e in out if isinstance(e, AgentToolComposingEvent)]
    # 50 dense deltas on a fake clock that does not advance → exactly the
    # first chunk emits (leading edge), the rest are throttled.
    assert len(composing) == 1
    assert composing[0].args_chunk == "part0 "


# ── 3. Engine-level integration (fake chat) ───────────────────────────────


class FakeComposingChat(BaseLLMChat):
    """Streams reasoning + tool-call deltas, then the full tool call."""

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
            yield ReasoningTokenEvent(content="I should write the file.")
            # Name arrives with the first args fragment (OpenAI behaviour).
            yield _delta(0, "write_file", '{"path": "src/new_file.py", ')
            yield _delta(0, "write_file", '"content": "def main():\\n    pass\\n')
            yield _delta(0, "write_file", '"language": "python"}')
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_compose_1",
                tool_name="write_file",
                args={"path": "src/new_file.py", "content": "def main():\n    pass\n"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=100, output=30),
                cost_usd=0.0001,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return

        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=150, output=10),
            cost_usd=0.0001,
            cost_known=True,
        )
        yield TokenEvent(content="Done.")
        yield LLMDoneEvent(content="Done.", has_tool_calls=False)


def _write_file_tool() -> list[Any]:
    from phoson_agent.models import AgentTool

    return [
        AgentTool(
            name="write_file",
            description="Writes a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=lambda args, context: f"wrote {args['path']}",
        )
    ]


@pytest.mark.asyncio
async def test_engine_composing_before_tool_start() -> None:
    import phoson_agent._loop as loop_mod

    # Deterministic clock: deltas are dense → only the first composing
    # event emits; the name-arrival chunk is also leading-edge.
    clock = _FakeClock()
    orig = loop_mod.time.monotonic
    loop_mod.time.monotonic = clock
    try:
        engine = AgentEngine(chat=FakeComposingChat(), tools=_write_file_tool())
        events = [
            event
            async for event in engine.stream(
                [Message(role="user", content="write a file")], CONFIG
            )
        ]
    finally:
        loop_mod.time.monotonic = orig

    composing = [e for e in events if isinstance(e, AgentToolComposingEvent)]
    starts = [e for e in events if isinstance(e, AgentToolStartEvent)]
    assert composing, "expected at least one composing event"
    assert starts, "expected the tool start event"
    # Composing must precede the first tool start.
    assert min(events.index(e) for e in composing) < events.index(starts[0])
    # The fixture's first fragment carries the name (OpenAI behaviour), so
    # every composing event is labelled with the real verb.
    assert composing[-1].tool_name == "write_file"
    # Tool start still carries the full args, and done follows.
    assert starts[0].tool_name == "write_file"
    assert starts[0].args["path"] == "src/new_file.py"
    assert any(isinstance(e, AgentToolDoneEvent) for e in events)
    assert any(isinstance(e, AgentDoneEvent) for e in events)


@pytest.mark.asyncio
async def test_engine_text_only_stream_emits_no_composing() -> None:
    class _TextOnlyChat(BaseLLMChat):
        async def stream(
            self,
            messages: list[Message],
            config: ModelConfig,
            tools: list[ToolDefinition] | None = None,
        ) -> AsyncIterator[LLMEvent]:
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield ReasoningTokenEvent(content="thinking")
            yield TokenEvent(content="Just text.")
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=False,
            )
            yield LLMDoneEvent(content="Just text.", has_tool_calls=False)

    engine = AgentEngine(chat=_TextOnlyChat())
    events = [event async for event in engine.stream(EMPTY_HISTORY, CONFIG)]
    assert AgentToolComposingEvent not in [type(e) for e in events]
    assert any(isinstance(e, AgentDoneEvent) for e in events)


@pytest.mark.asyncio
async def test_engine_error_mid_composing_surfaces_error_not_crash() -> None:
    class _FailingMidComposeChat(BaseLLMChat):
        async def stream(
            self,
            messages: list[Message],
            config: ModelConfig,
            tools: list[ToolDefinition] | None = None,
        ) -> AsyncIterator[LLMEvent]:
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield _delta(0, "bash", '{"command": "long build ')
            yield _delta(0, "bash", '--flag"}')
            yield ErrorEvent(message="stream cut", code="network", retryable=True)

    engine = AgentEngine(chat=_FailingMidComposeChat())
    events = [event async for event in engine.stream(EMPTY_HISTORY, CONFIG)]
    kinds = [type(e) for e in events]
    assert AgentToolComposingEvent in kinds
    assert AgentErrorEvent in kinds
    # No tool start: the call never completed, so nothing may have run.
    assert AgentToolStartEvent not in kinds
    assert AgentToolDoneEvent not in kinds
    # The error event is the terminal signal.
    assert isinstance(events[-1], AgentErrorEvent)
    assert events[-1].code == "network"


def test_composing_event_exported_from_package() -> None:
    """The new event is part of the public ``phoson_agent`` API."""
    import phoson_agent

    assert "AgentToolComposingEvent" in phoson_agent.__all__
    assert phoson_agent.AgentToolComposingEvent is AgentToolComposingEvent


def test_composing_event_default_fields() -> None:
    event = AgentToolComposingEvent(index=2, tool_name="bash", args_chunk='{"co')
    assert event.index == 2
    assert event.tool_call_id == ""
    assert event.tool_name == "bash"
    assert event.args_chunk == '{"co'
    assert event.timestamp is not None
