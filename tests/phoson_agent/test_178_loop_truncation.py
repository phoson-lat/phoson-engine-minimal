"""Tests for #178 — the agent loop side (F-13 truncation dispatch + F-14
middleware-exception pairing).

Design: the adapter that saw a tool call cut off mid-JSON marks it (``_truncated``
on a max_tokens cut, or ``_raw`` on an unparseable fallback). The
:class:`~phoson_agent._tool_runner.ToolRunner` is the enforcement boundary: it
refuses to dispatch a call with unusable args and answers it with an actionable
error result instead, so the handler is never invoked on partial JSON (F-13) and
a misbehaving middleware can't orphan a ``tool_use`` (F-14).

Acceptance criteria locked here:

1. *Truncation guard* — driving the real OpenAI-compatible adapter through a
   ``finish_reason == "length"`` stream cut mid tool-call: the handler is NOT
   invoked, the model receives an actionable error result, and the history stays
   paired.
2. *Middleware exception* — a middleware that raises ``RuntimeError`` in
   ``on_after_tool`` must not break the run; the history stays paired.
"""

from collections.abc import AsyncIterator

import pytest

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_agent.models import (
    AgentTool,
    AgentDoneEvent,
)
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.middleware import AgentMiddleware
from phoson_agent._tool_runner import ToolRunner, _is_unusable_args

# ─── F-13: max_tokens with a truncated tool call ─────────────────────────────


def _is_paired(history: list[Message]) -> bool:
    """True when every tool_use in the history has a later matching
    tool_result (the invariant that keeps a session valid for the provider)."""
    open_ids: set[str] = set()
    for msg in history:
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                open_ids.add(block.tool_call_id)
            elif isinstance(block, ToolResultBlock):
                open_ids.discard(block.tool_call_id)
    return not open_ids


# ── F-13 acceptance criterion: real OpenAI adapter, max_tokens mid tool-call ──


def _make_truncated_openai_client():
    """A fake AsyncOpenAI client whose stream is cut at max_tokens mid
    tool-call on the first call (partial JSON, ``finish_reason == "length"``) —
    exactly the stream the acceptance criterion describes. On the second call
    (the model's retry) it returns a normal final answer, so the run recovers
    instead of looping to ``max_iterations``."""

    state = {"calls": 0}

    class _Function:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _ToolCall:
        def __init__(self):
            self.index = 0
            self.id = "call_openai_1"
            self.function = _Function("get_weather", '{"city":"Q')  # partial

    class _DeltaTools:
        def __init__(self):
            self.content = None
            self.tool_calls = [_ToolCall()]

    class _DeltaText:
        def __init__(self):
            self.content = "En Qro esta soleado"
            self.tool_calls = None

    class _Choice:
        def __init__(self, delta, finish_reason):
            self.delta = delta
            self.finish_reason = finish_reason

    class _Chunk:
        def __init__(self, choice):
            self.choices = [choice]
            self.usage = None

    class _Stream:
        def __init__(self, chunk):
            self._chunk = chunk

        def __aiter__(self):
            async def _iter():
                yield self._chunk

            return _iter()

    class _Completions:
        async def create(self, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                # First turn: truncated mid tool-call.
                return _Stream(_Chunk(_Choice(_DeltaTools(), "length")))
            # Second turn (retry): a normal final answer.
            return _Stream(_Chunk(_Choice(_DeltaText(), "stop")))

    class _Chat:
        completions = _Completions()

    return type("FakeClient", (), {"chat": _Chat()})()


@pytest.mark.asyncio
async def test_openai_max_tokens_truncated_tool_call_not_invoked() -> None:
    """Acceptance criterion: a stream with stop_reason=max_tokens and partial
    tool-call JSON must NOT invoke the handler; the model receives an
    actionable error result and the history stays paired."""
    from phoson_llm.chats.openai import OpenAIChat

    counter: list[str] = []

    @tool
    def get_weather(city: str) -> dict:  # noqa: D103
        counter.append(city)
        return {"city": city, "condition": "sunny", "temperature_c": 27}

    chat = OpenAIChat(api_key="test")
    # Inject a fake AsyncOpenAI client whose stream is cut at max_tokens
    # mid tool-call (partial JSON, finish_reason == "length").
    chat._client = _make_truncated_openai_client()

    engine = AgentEngine(chat=chat, tools=[get_weather], max_iterations=3)

    result = await engine.run(
        messages=[Message(role="user", content="clima")],
        config=ModelConfig(model="gpt-4o-mini", max_tokens=16),
    )

    # The handler was NEVER invoked on the partial-JSON call.
    assert counter == [], "truncated tool call must not invoke the handler"

    # The single tool_use is answered with an actionable, error tool_result.
    tool_uses = [
        b
        for m in result.history
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert len(tool_uses) == 1
    assert tool_uses[0].tool_call_id == "call_openai_1"
    assert tool_uses[0].args.get("_truncated") is True

    tool_results = [
        b
        for m in result.history
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "call_openai_1"
    assert tool_results[0].error is True
    assert "not executed" in tool_results[0].result.lower()
    # Actionable: tells the model what to do next (retry smaller).
    lowered = tool_results[0].result.lower()
    assert "retry" in lowered or "split" in lowered

    # History is paired — the persisted session is valid for the provider.
    assert _is_paired(result.history)


# ── F-13 runner boundary: _raw (unparseable) args are also refused ────────────


async def _passthrough(call):
    return call


async def _passthrough_after(call, result, error):  # noqa: ARG001
    return result


async def _passthrough_event(event):
    return event


def test_is_unusable_args_detects_truncated_and_raw() -> None:
    assert _is_unusable_args({"_truncated": True}) is True
    assert _is_unusable_args({"_raw": '{"x":'}) is True
    assert _is_unusable_args({"a": 1}) is False
    assert _is_unusable_args({}) is False
    assert _is_unusable_args("not a dict") is False


@pytest.mark.asyncio
async def test_runner_refuses_raw_fallback_args() -> None:
    """A tool call whose args fell back to the ``_raw`` marker (unparseable
    JSON) is answered with an actionable error, never dispatched to the
    handler — the pre-existing path that produced the opaque ``fn(_raw=...)``
    TypeError (F-13)."""
    called: list[str] = []

    @tool
    def echo(x: str) -> str:  # noqa: D103
        called.append(x)
        return x

    runner = ToolRunner(
        tools_by_name={"echo": echo},
        context=AgentContext(),
        apply_before_tool=_passthrough,
        apply_after_tool=_passthrough_after,
        prepare_event=_passthrough_event,
    )
    history: list[Message] = []
    steps: list = []
    call = ToolCallEvent(
        index=0,
        tool_call_id="t_raw",
        tool_name="echo",
        args={"_raw": '{"x": 1,'},  # partial, unparseable
    )
    await _drain(runner.execute(tool_calls=[call], history=history, steps=steps))

    assert called == [], "_raw args must not reach the handler"
    assert len(steps) == 1
    assert steps[0].error == "tool_call_truncated"
    block = history[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.error is True
    assert "not executed" in block.result.lower()


async def _drain(ait) -> list:
    return [ev async for ev in ait]


# ─── F-14: middleware raising in on_after_tool must not break the run ────────


class _RaisingAfterTool(AgentMiddleware):
    """A middleware whose ``on_after_tool`` hook raises — models a buggy
    middleware (or a non-Phoson error escaping a hook)."""

    async def on_after_tool(self, call, result: str, error: bool) -> str:
        raise RuntimeError("middleware exploded")


class _TwoTurnToolChat(BaseLLMChat):
    """First turn: a tool call; second turn: the final answer."""

    def __init__(self) -> None:
        self._iteration = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMEvent]:
        self._iteration += 1
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        if self._iteration == 1:
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_mid_1",
                tool_name="get_weather",
                args={"city": "Qro"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=False,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=20, output=10),
            cost_usd=0.0,
            cost_known=False,
        )
        yield LLMDoneEvent(content="final", has_tool_calls=False)


def _weather_tool(counter: list[str]) -> AgentTool:
    @tool
    def get_weather(city: str) -> dict:  # noqa: D103
        counter.append(city)
        return {"city": city, "condition": "sunny", "temperature_c": 27}

    return get_weather


@pytest.mark.asyncio
async def test_middleware_runtime_error_in_on_after_tool_keeps_history_paired() -> None:
    """A RuntimeError raised in on_after_tool must NOT break the run: the tool
    call is answered with a paired error result (exception type included), the
    run completes, and the history stays valid for the provider."""
    counter: list[str] = []
    get_weather = _weather_tool(counter)

    engine = AgentEngine(
        chat=_TwoTurnToolChat(),
        tools=[get_weather],
        middlewares=[_RaisingAfterTool()],
        max_iterations=4,
    )
    # Must NOT raise — the middleware error is converted to a tool result.
    result = await engine.run(
        messages=[Message(role="user", content="clima")],
        config=ModelConfig(model="fake", max_tokens=64),
    )

    assert result.final_content == "final"
    assert _is_paired(result.history)

    # The tool DID run (handler was called), but the result was replaced by the
    # middleware error, which names the exception type.
    assert counter == ["Qro"]

    tool_results = [
        b
        for m in result.history
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "call_mid_1"
    assert tool_results[0].error is True
    assert "RuntimeError" in tool_results[0].result
    assert "middleware exploded" in tool_results[0].result


@pytest.mark.asyncio
async def test_middleware_runtime_error_in_on_after_tool_steps_record_error() -> None:
    """The RunStep for the affected tool call records the middleware error so
    /details and tracing can show *why* it failed."""
    counter: list[str] = []
    get_weather = _weather_tool(counter)

    engine = AgentEngine(
        chat=_TwoTurnToolChat(),
        tools=[get_weather],
        middlewares=[_RaisingAfterTool()],
        max_iterations=4,
    )
    result = await engine.run(
        messages=[Message(role="user", content="clima")],
        config=ModelConfig(model="fake", max_tokens=64),
    )

    tool_steps = [s for s in result.steps if s.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].tool_call_id == "call_mid_1"
    assert tool_steps[0].error is not None
    assert "RuntimeError" in tool_steps[0].error


# ─── F-13: max_tokens final answer with no tool call → result.truncated ──────


class _TruncatedFinalChat(BaseLLMChat):
    """A single turn whose final answer is cut at max_tokens with no tool call
    — the case the issue wants the UI to flag as 'respuesta truncada'."""

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=10, output=5),
            cost_usd=0.0,
            cost_known=False,
        )
        yield LLMDoneEvent(
            content="respuesta muy larga que se cort",
            has_tool_calls=False,
            stop_reason="max_tokens",
        )


@pytest.mark.asyncio
async def test_max_tokens_final_answer_flags_result_truncated() -> None:
    """A final answer cut at max_tokens (no tool call) sets result.truncated so
    the UI can flag it as an incomplete answer rather than a clean completion."""
    engine = AgentEngine(chat=_TruncatedFinalChat(), tools=[], max_iterations=2)
    result = await engine.run(
        messages=[Message(role="user", content="write something long")],
        config=ModelConfig(model="fake", max_tokens=8),
    )
    assert result.final_content == "respuesta muy larga que se cort"
    assert result.truncated is True


@pytest.mark.asyncio
async def test_normal_final_answer_not_truncated() -> None:
    """A clean completion (stop_reason end_turn) is not flagged truncated."""

    class _CleanChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield LLMDoneEvent(
                content="done", has_tool_calls=False, stop_reason="end_turn"
            )

    engine = AgentEngine(chat=_CleanChat(), tools=[], max_iterations=2)
    result = await engine.run(
        messages=[Message(role="user", content="hi")],
        config=ModelConfig(model="fake", max_tokens=8),
    )
    assert result.truncated is False


# ─── F-13 UI: render_done_line flags a truncated run ──────────────────────────


def test_render_done_line_flags_truncated() -> None:
    from phoson_cli.theme import load_theme
    from phoson_llm.schemas import Message
    from phoson_agent.models import AgentRunResult
    from phoson_cli.formatting import render_done_line

    theme = load_theme("dark")

    truncated = AgentRunResult(
        final_content="x",
        history=[Message(role="assistant", content="x")],
        input_messages=[],
        total_cost_usd=0.12345,
        truncated=True,
    )
    line = render_done_line(AgentDoneEvent(result=truncated), theme)
    assert line is not None
    assert "truncated" in line.plain
    # The cost + step count are still shown alongside the truncation badge.
    assert "step" in line.plain

    clean = AgentRunResult(
        final_content="x",
        history=[Message(role="assistant", content="x")],
        input_messages=[],
        total_cost_usd=0.12345,
        truncated=False,
    )
    clean_line = render_done_line(AgentDoneEvent(result=clean), theme)
    assert clean_line is not None
    assert "truncated" not in clean_line.plain
