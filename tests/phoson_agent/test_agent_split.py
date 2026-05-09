"""Tests for the agent loop / tool runner / internals modules.

These exercise the boundaries that were introduced when ``agent.py``
was split into ``_internals.py``, ``_loop.py`` and ``_tool_runner.py``.
We don't replicate the full integration tests in
``test_agent_engine_integration.py`` — those already cover the
end-to-end behaviour. Instead we lock in the contracts that each
module exposes so future refactors don't accidentally drop a hook.
"""

import asyncio
import datetime
from collections.abc import AsyncIterator

import pytest

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
    ReasoningTokenEvent,
)
from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_agent.context import AgentContext
from phoson_agent._internals import (
    LLMStepOutcome,
    IterationCost,
    IterationFinal,
    IterationFailed,
    now_utc,
    duration_ms,
    subagent_label,
    to_result_text,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent._tool_runner import ToolRunner


# ─── _internals helpers ──────────────────────────────────────────────────────


def test_now_utc_returns_aware_datetime() -> None:
    ts = now_utc()
    assert ts.tzinfo == datetime.UTC


def test_duration_ms_handles_subsecond_precision() -> None:
    a = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    b = a + datetime.timedelta(milliseconds=42)
    assert duration_ms(a, b) == 42


def test_to_result_text_dict_is_ascii_json() -> None:
    assert to_result_text({"x": 1}) == '{"x": 1}'


def test_subagent_label_recognises_known_tools() -> None:
    assert subagent_label("agent") == "subagent"
    assert subagent_label("agents") == "subagents"
    assert subagent_label("bash") is None


# ─── Sentinels are dataclass instances of AgentEvent ─────────────────────────


def test_iteration_sentinels_carry_payloads() -> None:
    cost = IterationCost(cost_usd=0.5, credits=0.6)
    assert cost.cost_usd == 0.5
    assert cost.credits == 0.6

    final = IterationFinal(final_content="hi")
    assert final.final_content == "hi"

    failed = IterationFailed()
    assert failed.error_event is not None  # default factory


# ─── LLMStepOutcome aggregates the right event types ─────────────────────────


def test_llm_step_outcome_starts_empty() -> None:
    outcome = LLMStepOutcome()
    assert outcome.tool_calls == []
    assert outcome.usage_event is None
    assert outcome.done_event is None
    assert outcome.error_event is None


# ─── End-to-end: the public API still works after the split ──────────────────


class _FakeChat(BaseLLMChat):
    """Minimal chat that emits a token + usage + done sequence."""

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield TokenEvent(content="hi")
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=3, output=2),
            cost_usd=0.001,
            cost_known=True,
        )
        yield LLMDoneEvent(content="hi", has_tool_calls=False)


@pytest.mark.asyncio
async def test_engine_drives_loop_and_tool_runner_together() -> None:
    engine = AgentEngine(chat=_FakeChat(), tools=[], max_iterations=1)
    result = await engine.run(
        messages=[Message(role="user", content="hello")],
        config=ModelConfig(model="fake", max_tokens=8),
    )
    assert result.final_content == "hi"
    assert result.total_cost_usd == 0.001
    # One LLM step recorded, no tool steps.
    assert len(result.steps) == 1
    assert result.steps[0].kind == "llm"


# ─── ToolRunner can be exercised standalone ──────────────────────────────────


@pytest.mark.asyncio
async def test_tool_runner_invokes_handler_and_records_step() -> None:
    @tool
    def echo(text: str) -> str:
        return text

    async def passthrough_before(call):
        return call

    async def passthrough_after(call, result, error):  # noqa: ARG001
        return result

    async def passthrough_event(event):
        return event

    runner = ToolRunner(
        tools_by_name={"echo": echo},
        context=AgentContext(),
        apply_before_tool=passthrough_before,
        apply_after_tool=passthrough_after,
        prepare_event=passthrough_event,
    )

    from phoson_llm.schemas import ToolCallEvent

    history: list[Message] = []
    steps: list = []
    call = ToolCallEvent(
        index=0,
        tool_call_id="t1",
        tool_name="echo",
        args={"text": "hola"},
    )

    events = [
        ev
        async for ev in runner.execute(tool_calls=[call], history=history, steps=steps)
    ]

    # Start + Done + StepDone = 3 events for a single tool.
    assert len(events) == 3
    assert len(steps) == 1
    assert steps[0].kind == "tool"
    assert steps[0].tool_name == "echo"
    # The tool result block was appended to history.
    assert len(history) == 1


@pytest.mark.asyncio
async def test_tool_runner_blocks_when_middleware_returns_none() -> None:
    @tool
    def noop() -> str:
        return "should not run"

    async def block_before(_call):
        return None

    async def passthrough_after(call, result, error):  # noqa: ARG001
        return result

    async def passthrough_event(event):
        return event

    runner = ToolRunner(
        tools_by_name={"noop": noop},
        context=AgentContext(),
        apply_before_tool=block_before,
        apply_after_tool=passthrough_after,
        prepare_event=passthrough_event,
    )

    from phoson_llm.schemas import ToolCallEvent

    history: list[Message] = []
    steps: list = []
    call = ToolCallEvent(
        index=0,
        tool_call_id="t1",
        tool_name="noop",
        args={},
    )

    events = [
        ev
        async for ev in runner.execute(tool_calls=[call], history=history, steps=steps)
    ]

    assert len(events) == 2  # ToolDone + StepDone (no Start)
    assert steps[0].error == "blocked_by_middleware"


# ─── Cancellation backfills synthetic results ────────────────────────────────


def test_fill_cancelled_results_appends_synthetic_blocks() -> None:
    """The private ``_fill_cancelled_results`` must produce one tool_result
    block per pending call so the LLM history never has orphaned tool_use blocks.
    """
    from phoson_llm.schemas import ToolCallEvent, ToolResultBlock

    runner = ToolRunner(
        tools_by_name={},
        context=AgentContext(),
        apply_before_tool=_passthrough,
        apply_after_tool=_passthrough_after,
        prepare_event=_passthrough_event,
    )
    history: list[Message] = []
    pending = [
        ToolCallEvent(index=i, tool_call_id=f"t{i}", tool_name="slow", args={})
        for i in range(3)
    ]

    runner._fill_cancelled_results(history, pending)

    assert len(history) == 3
    for msg, call in zip(history, pending, strict=True):
        assert msg.role == "user"
        block = msg.content[0]
        assert isinstance(block, ToolResultBlock)
        assert block.tool_call_id == call.tool_call_id
        assert block.error is True
        assert "cancelled" in block.result.lower()


async def _passthrough(call):
    return call


async def _passthrough_after(call, result, error):  # noqa: ARG001
    return result


async def _passthrough_event(event):
    return event


def test_engine_does_not_re_export_internal_sentinels() -> None:
    """The internal sentinels live in ``_internals`` and stay private.

    Regression: while splitting ``agent.py`` we considered re-exporting
    them; that would have leaked the internal protocol into the public
    surface. This test pins them down.
    """
    import phoson_agent

    assert not hasattr(phoson_agent, "IterationCost")
    assert not hasattr(phoson_agent, "IterationFinal")
    assert not hasattr(phoson_agent, "IterationFailed")
