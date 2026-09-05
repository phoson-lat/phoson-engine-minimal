"""E2E: AgentEngine + PhosonOtelPlugin (issue #140).

Exercises the full path — engine events → middleware span tree →
file sink — with a fake chat (no network), and proves the
shared-middleware-instance setup that the CLI uses for sub-agents
(#174/F-01): two engines (or concurrent tasks) share *one*
``OtelTracingMiddleware`` and still produce two clean, separate
traces.
"""

import json
import asyncio
from pathlib import Path
from collections.abc import AsyncIterator

import pytest

from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_plugin_otel import PhosonOtelPlugin
from phoson_agent.models import AgentTool
from phoson_llm.chats.base import BaseLLMChat
from phoson_plugin_otel.sink import PHOSON_TRACE_KEY


class _ToolThenAnswerChat(BaseLLMChat):
    """Iteration 1: emit a tool call. Iteration 2: final answer."""

    def __init__(self, tool_name: str = "bash", tool_args: dict | None = None) -> None:
        self.tool_name = tool_name
        self.tool_args = tool_args or {"command": "ls"}
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
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_e2e_1",
                tool_name=self.tool_name,
                args=self.tool_args,
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=120, output=28),
                cost_usd=0.00042,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=180, output=46),
            cost_usd=0.00067,
            cost_known=True,
        )
        yield LLMDoneEvent(content="All done.", has_tool_calls=False)


class _AlwaysToolChat(BaseLLMChat):
    """Never emits a final answer — every iteration is a tool call."""

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield ToolCallEvent(
            index=0,
            tool_call_id="call_loop",
            tool_name="bash",
            args={"command": "true"},
        )
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=10, output=4),
            cost_usd=0.0001,
            cost_known=True,
        )
        yield LLMDoneEvent(content="", has_tool_calls=True)


def _bash_tool() -> AgentTool:
    return AgentTool(
        name="bash",
        description="Run a shell command.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        handler=lambda args, context=None: "file listing",
    )


def _plugin_with_file(tmp_path: Path, name: str = "t.json") -> PhosonOtelPlugin:
    plugin = PhosonOtelPlugin()
    plugin.configure({"service_name": "phoson-e2e", "file_path": str(tmp_path / name)})
    return plugin


def _read_doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _spans_by_name(doc: dict) -> list[dict]:
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"]


class TestEngineE2E:
    @pytest.mark.asyncio
    async def test_full_run_produces_trace_file(self, tmp_path: Path) -> None:
        plugin = _plugin_with_file(tmp_path)
        target = tmp_path / "t.json"
        engine = AgentEngine(
            chat=_ToolThenAnswerChat(),
            tools=[_bash_tool()],
            plugins=[plugin],
            max_iterations=5,
        )
        result = await engine.run(
            [Message(role="user", content="do it")],
            ModelConfig(model="fake-model"),
        )

        assert [s.kind for s in result.steps] == ["llm", "tool", "llm"]
        doc = _read_doc(target)
        spans = _spans_by_name(doc)
        names = [s["name"] for s in spans]
        assert names == [
            "phoson.run",
            "phoson.step",
            "phoson.llm_call",
            "phoson.step",
            "phoson.tool_call",
            "phoson.step",
            "phoson.llm_call",
        ]

        # Hierarchy is valid: every non-root span's parent exists.
        by_id = {s["spanId"]: s for s in spans}
        for span in spans:
            parent = span.get("parentSpanId")
            if parent:
                assert parent in by_id

        # Token/cost attributes match the RunSteps the engine reported
        # (the /cost-accuracy criterion from #140).
        llm_spans = [s for s in spans if s["name"] == "phoson.llm_call"]
        total_cost = 0.0
        for span, step in zip(llm_spans, [s for s in result.steps if s.kind == "llm"]):
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            assert (
                int(attrs["gen_ai.usage.input_tokens"]["intValue"]) == step.usage.input
            )
            assert (
                int(attrs["gen_ai.usage.output_tokens"]["intValue"])
                == step.usage.output
            )
            assert attrs["gen_ai.request.model"]["stringValue"] == step.model
            total_cost += step.cost_usd
        run_span = spans[0]
        run_attrs = {a["key"]: a["value"] for a in run_span["attributes"]}
        assert run_attrs["phoson.total_cost_usd"]["doubleValue"] == pytest.approx(
            total_cost
        )
        assert int(run_attrs["phoson.step_count"]["intValue"]) == 3
        assert doc[PHOSON_TRACE_KEY]["trace_id"] == spans[0]["traceId"]
        assert all(s["traceId"] == spans[0]["traceId"] for s in spans)

    @pytest.mark.asyncio
    async def test_error_run_exports_error_trace(self, tmp_path: Path) -> None:
        """A run that fails (max_iterations) still exports, with the
        run span marked ERROR (attribution criterion, #140)."""
        target = tmp_path / "t.json"
        plugin = _plugin_with_file(tmp_path)
        engine = AgentEngine(
            chat=_AlwaysToolChat(),
            tools=[_bash_tool()],
            plugins=[plugin],
            max_iterations=2,
        )
        with pytest.raises(Exception):
            await engine.run(
                [Message(role="user", content="loop")],
                ModelConfig(model="fake-model"),
            )
        assert target.exists(), "failed run must still export a trace"
        doc = _read_doc(target)
        run_span = _spans_by_name(doc)[0]
        assert run_span["status"]["code"] == 2  # ERROR


class TestSharedMiddlewareSubagents:
    """The CLI hands the *same* middleware list (same instances) to the
    parent engine and every sub-agent engine. With shared instances,
    concurrent runs must still isolate cleanly."""

    @pytest.mark.asyncio
    async def test_two_engines_one_middleware(self, tmp_path: Path) -> None:
        # Configure *before* building the engines so both engines share
        # the one middleware instance the plugin created.
        plugin = _plugin_with_file(tmp_path)
        plugin.configure({"file_path": str(tmp_path / "{trace_id}.json")})
        middlewares = plugin.get_middlewares()

        engine_a = AgentEngine(
            chat=_ToolThenAnswerChat(),
            tools=[_bash_tool()],
            middlewares=list(middlewares),
            max_iterations=5,
        )
        engine_b = AgentEngine(
            chat=_ToolThenAnswerChat(tool_name="bash"),
            tools=[_bash_tool()],
            middlewares=list(middlewares),
            max_iterations=5,
        )

        await asyncio.gather(
            engine_a.run([Message(role="user", content="A")], ModelConfig(model="m-a")),
            engine_b.run([Message(role="user", content="B")], ModelConfig(model="m-b")),
        )

        # {trace_id}.json → one file per run.
        files = [p for p in tmp_path.glob("*.json")]
        assert len(files) == 2, f"expected 2 trace files, got {[f.name for f in files]}"
        trace_ids = set()
        for f in files:
            doc = _read_doc(f)
            spans = _spans_by_name(doc)
            names = [s["name"] for s in spans]
            assert names[0] == "phoson.run"
            assert names.count("phoson.step") == 3
            trace_ids.add(doc[PHOSON_TRACE_KEY]["trace_id"])
        assert len(trace_ids) == 2, "the two runs must not share a trace id"

    def test_serial_subagent_runs_do_not_leak_state(self, tmp_path: Path) -> None:
        """Sequential sub-agent runs (the common case) each get their own
        trace even though the middleware instance is shared."""
        plugin = _plugin_with_file(tmp_path)
        target = tmp_path / "{trace_id}.json"
        plugin.configure({"file_path": str(target)})
        middlewares = plugin.get_middlewares()

        async def _one() -> AgentEngine:
            engine = AgentEngine(
                chat=_ToolThenAnswerChat(),
                tools=[_bash_tool()],
                middlewares=list(middlewares),
                max_iterations=5,
            )
            await engine.run(
                [Message(role="user", content="sub")], ModelConfig(model="sub")
            )
            return engine

        asyncio.run(_one())
        asyncio.run(_one())

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 2
        trace_ids = {_read_doc(f)[PHOSON_TRACE_KEY]["trace_id"] for f in files}
        assert len(trace_ids) == 2
