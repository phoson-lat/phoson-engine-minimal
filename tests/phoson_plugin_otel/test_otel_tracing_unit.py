"""Unit tests for OtelTracingMiddleware (issue #140).

Feeds the *real* agent event types through the middleware and asserts
the run → step → llm_call/tool_call span tree, the attribute contents
(model/tokens/cost/tool outcome) and the per-run isolation that makes
the shared-instance + parallel-sub-agent setup safe.
"""

import datetime
from datetime import UTC

import pytest

from phoson_llm.schemas import TokenUsage
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentStepDoneEvent,
)
from phoson_plugin_otel.tracing import OtelTracingMiddleware, _RunState


def _now(offset_s: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC) + datetime.timedelta(
        seconds=offset_s
    )


def _llm_step(
    offset: int = 0,
    *,
    model: str = "test-model",
    usage: TokenUsage | None = None,
    cost: float = 0.01,
    error: str | None = None,
) -> RunStep:
    return RunStep(
        kind="llm",
        started_at=_now(offset),
        ended_at=_now(offset + 1),
        duration_ms=1000,
        model=model,
        usage=usage,
        cost_usd=cost,
        credits=cost * 1.1,
        error=error,
        payload={},
    )


def _tool_step(
    offset: int = 0,
    *,
    tool_name: str = "bash",
    call_id: str = "call-1",
    args: dict | None = None,
    result: str = "ok",
    error: str | None = None,
) -> RunStep:
    return RunStep(
        kind="tool",
        started_at=_now(offset),
        ended_at=_now(offset + 2),
        duration_ms=2000,
        tool_name=tool_name,
        tool_call_id=call_id,
        error=error,
        payload={
            "args": args if args is not None else {"command": "ls"},
            "result": result,
        },
    )


def _make_mw(collected: list[_RunState]) -> OtelTracingMiddleware:
    return OtelTracingMiddleware(collected.append)


def _attrs(span) -> dict:
    """Invert the rendered OTLP attribute list into a {key: leaf} dict."""
    out = {}
    for attr in span.to_otlp_json()["attributes"]:
        out[attr["key"]] = _leaf(attr["value"])
    return out


def _leaf(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "kvlistValue" in v:
        return {k["key"]: _leaf(k["value"]) for k in v["kvlistValue"]["values"]}
    if "arrayValue" in v:
        return [_leaf(x) for x in v["arrayValue"]["values"]]
    return v


def _run_and_collect(collected) -> None:
    mw = _make_mw(collected)
    mw._on_start(AgentStartEvent(model="test-model", message_count=2, max_iterations=5))
    mw._on_step(_llm_step(0, usage=TokenUsage(input=10, output=5), cost=0.01))
    mw._on_step(_tool_step(1))
    mw._on_step(_llm_step(3, usage=TokenUsage(input=20, output=8), cost=0.02))
    mw._on_done(
        AgentDoneEvent(
            result=AgentRunResult(
                final_content="done",
                history=[],
                input_messages=[],
                total_cost_usd=0.03,
                total_credits=0.033,
            )
        )
    )


class TestSpanTree:
    def test_hierarchy_names(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        names = [s.name for s in state.spans]
        assert names == [
            "phoson.run",
            "phoson.step",
            "phoson.llm_call",
            "phoson.step",
            "phoson.tool_call",
            "phoson.step",
            "phoson.llm_call",
        ]

    def test_parent_links(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        spans = state.spans
        run, step1, llm1, step2, tool1, step3, llm2 = spans
        assert step1.parent_id == run.span_id
        assert llm1.parent_id == step1.span_id
        assert tool1.parent_id == step2.span_id
        assert llm2.parent_id == step3.span_id
        assert all(s.trace_id == run.trace_id for s in spans)
        assert run.parent_id == ""

    def test_run_span_attributes(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        attrs = _attrs(state.run_span)
        assert attrs["phoson.model"] == "test-model"
        assert attrs["phoson.message_count"] == 2
        assert attrs["phoson.max_iterations"] == 5
        assert attrs["phoson.step_count"] == 3
        assert attrs["phoson.total_cost_usd"] == pytest.approx(0.03)
        assert state.run_span.status == 1  # OK
        assert "phoson.run.id" in state.run_span.attributes

    def test_llm_span_attributes(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        llm1 = state.spans[2]
        attrs = _attrs(llm1)
        assert attrs["gen_ai.request.model"] == "test-model"
        assert attrs["gen_ai.usage.input_tokens"] == 10
        assert attrs["gen_ai.usage.output_tokens"] == 5
        assert attrs["gen_ai.usage.cache_read_tokens"] == 0
        assert attrs["phoson.cost_usd"] == pytest.approx(0.01)
        assert llm1.kind == 3  # CLIENT
        assert llm1.status == 1

    def test_tool_span_attributes(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        tool1 = state.spans[4]
        attrs = _attrs(tool1)
        assert attrs["phoson.tool.name"] == "bash"
        assert attrs["phoson.tool.call_id"] == "call-1"
        assert attrs["phoson.tool.outcome"] == "ok"
        assert attrs["phoson.tool.args"] == {"command": "ls"}
        assert attrs["phoson.tool.result_chars"] == 2
        assert tool1.status == 1

    def test_step_span_attributes(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        step1 = state.spans[1]
        attrs = _attrs(step1)
        assert attrs["phoson.step.index"] == 0
        assert attrs["phoson.step.kind"] == "llm"
        assert attrs["phoson.step.duration_ms"] == 1000

    def test_step_timestamps_from_runstep(self) -> None:
        collected: list[_RunState] = []
        _run_and_collect(collected)
        (state,) = collected
        llm1 = state.spans[2]
        # 1 s apart, as the RunStep said (not wall-clock).
        assert llm1.end_time - llm1.start_time == 1_000_000_000


class TestOutcomes:
    def test_error_llm_step_marks_error(self) -> None:
        collected: list[_RunState] = []
        mw = _make_mw(collected)
        mw._on_start(AgentStartEvent(model="m"))
        mw._on_step(_llm_step(error="[timeout] provider timeout"))
        mw._on_error(AgentErrorEvent(message="boom", code="timeout"))
        (state,) = collected
        step, llm = state.spans[1], state.spans[2]
        assert step.status == 2  # ERROR
        assert llm.status == 2
        assert state.run_span.status == 2
        run_attrs = _attrs(state.run_span)
        assert run_attrs["phoson.error.code"] == "timeout"

    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (None, "ok"),
            ("blocked_by_middleware", "denied_by_middleware"),
            ("permission_denied", "denied_by_permission"),
            ("unusable_args", "unusable_args"),
            ("[handler] boom", "error"),
        ],
    )
    def test_tool_outcome_mapping(self, error, outcome) -> None:
        collected: list[_RunState] = []
        mw = _make_mw(collected)
        mw._on_start(AgentStartEvent(model="m"))
        mw._on_step(_tool_step(error=error))
        mw._on_done(AgentDoneEvent(result=AgentRunResult("x", [], [])))
        (state,) = collected
        attrs = _attrs(state.spans[2])
        assert attrs["phoson.tool.outcome"] == outcome

    def test_long_tool_args_are_clipped(self) -> None:
        collected: list[_RunState] = []
        mw = _make_mw(collected)
        mw._on_start(AgentStartEvent(model="m"))
        big = "x" * 2000
        mw._on_step(_tool_step(args={"command": big, "n": 7}))
        mw._on_done(AgentDoneEvent(result=AgentRunResult("x", [], [])))
        (state,) = collected
        attrs = _attrs(state.spans[2])
        assert attrs["phoson.tool.args"]["command"].endswith("...")
        assert len(attrs["phoson.tool.args"]["command"]) <= 500
        assert attrs["phoson.tool.args"]["n"] == 7


class TestRunIsolation:
    def test_steps_before_start_are_ignored(self) -> None:
        collected: list[_RunState] = []
        mw = _make_mw(collected)
        mw._on_step(_llm_step())  # no run open → dropped
        assert collected == []

    def test_export_is_idempotent(self) -> None:
        collected: list[_RunState] = []
        mw = _make_mw(collected)
        mw._on_start(AgentStartEvent(model="m"))
        mw._on_done(AgentDoneEvent(result=AgentRunResult("x", [], [])))
        # A second Done/Error on the same run (same instance, same
        # context) must not double-export.
        mw._on_error(AgentErrorEvent(message="late"))
        mw._on_done(AgentDoneEvent(result=AgentRunResult("y", [], [])))
        assert len(collected) == 1

    def test_contextvar_isolates_nested_runs(self) -> None:
        """A sub-agent run started inside a parent run (same middleware
        instance, separate context) must not clobber the parent state."""
        collected: list[_RunState] = []
        mw = _make_mw(collected)

        import contextvars

        parent_token_state: dict = {}

        def parent_run() -> None:
            mw._on_start(AgentStartEvent(model="parent"))
            parent_token_state["state"] = mw._current.get()
            # Simulate a sub-agent: new context (task.copy_context()).
            sub_ctx = contextvars.copy_context()

            def child_run() -> None:
                mw._on_start(AgentStartEvent(model="child"))
                mw._on_step(_tool_step())
                mw._on_done(AgentDoneEvent(result=AgentRunResult("c", [], [])))

            sub_ctx.run(child_run)
            # Parent context still sees the parent state.
            parent_token_state["state_after"] = mw._current.get()

        parent_run()

        states = list(collected)
        # Child was exported on its Done; parent not yet (no Done fired).
        child_states = [
            s for s in states if s.run_span.attributes["phoson.model"] == "child"
        ]
        assert len(child_states) == 1
        assert parent_token_state["state"] is parent_token_state["state_after"]
        assert (
            parent_token_state["state"].run_span.attributes["phoson.model"] == "parent"
        )


class TestMiddlewareHook:
    @pytest.mark.asyncio
    async def test_on_agent_event_routes(self) -> None:
        collected: list[_RunState] = []
        mw = _make_mw(collected)
        await mw.on_agent_event(AgentStartEvent(model="m"))
        await mw.on_agent_event(AgentStepDoneEvent(step=_llm_step()))
        await mw.on_agent_event(AgentDoneEvent(result=AgentRunResult("x", [], [])))
        assert len(collected) == 1

    @pytest.mark.asyncio
    async def test_on_agent_event_never_raises(self) -> None:
        """A broken exporter must not leak out of the hook."""

        def boom(_state) -> None:
            raise RuntimeError("disk on fire")

        mw = OtelTracingMiddleware(boom)
        # No exception propagates; the run is simply lost.
        await mw.on_agent_event(AgentStartEvent(model="m"))
        await mw.on_agent_event(AgentDoneEvent(result=AgentRunResult("x", [], [])))

    @pytest.mark.asyncio
    async def test_unknown_event_types_are_ignored(self) -> None:
        from phoson_agent.models import AgentTokenEvent

        mw = _make_mw([])
        await mw.on_agent_event(AgentTokenEvent(content="hi"))
