"""Tests for the permission-decision OTel audit trail (issue #227 phase 3).

The permission gate is a *different* middleware from the tracing one, so its
``on_decision`` sink calls ``OtelTracingMiddleware.record_permission`` directly;
each decision becomes a ``phoson.permission`` child span of the active run.
"""

from __future__ import annotations

from phoson_plugin_otel import PhosonOtelPlugin
from phoson_agent.models import AgentDoneEvent, AgentRunResult, AgentStartEvent
from phoson_agent.permissions import PermissionDecision
from phoson_cli.session_utils import record_permission_decision
from phoson_plugin_otel.tracing import OtelTracingMiddleware, _RunState


def _decision(**overrides) -> PermissionDecision:
    base = dict(
        tool_name="bash",
        level="deny",
        source="intent",
        allowed=False,
        reason="denied by permissions policy",
        intents=("filesystem_delete",),
        arg_digest="abc123",
    )
    base.update(overrides)
    return PermissionDecision(**base)


def _start_run(mw: OtelTracingMiddleware) -> None:
    mw._on_start(AgentStartEvent(model="test-model", message_count=1, max_iterations=5))


def _attrs(span) -> dict:
    out: dict = {}
    for attr in span.to_otlp_json()["attributes"]:
        value = attr["value"]
        out[attr["key"]] = value.get("stringValue", value.get("boolValue"))
    return out


def test_record_permission_outside_a_run_is_a_noop() -> None:
    mw = OtelTracingMiddleware(lambda _state: None)
    # No AgentStartEvent seen → no active run → must not raise or buffer.
    mw.record_permission(_decision())


def test_record_permission_adds_a_child_span() -> None:
    collected: list[_RunState] = []
    mw = OtelTracingMiddleware(collected.append)
    _start_run(mw)
    mw.record_permission(_decision())

    state = mw._current.get()
    assert state is not None
    permission_spans = [s for s in state.spans if s.name == "phoson.permission"]
    assert len(permission_spans) == 1
    span = permission_spans[0]
    assert span.parent_id == state.run_span.span_id
    assert span.trace_id == state.run_span.trace_id
    attrs = _attrs(span)
    assert attrs["phoson.permission.tool"] == "bash"
    assert attrs["phoson.permission.intents"] == "filesystem_delete"
    assert attrs["phoson.permission.level"] == "deny"
    assert attrs["phoson.permission.source"] == "intent"
    assert attrs["phoson.permission.allowed"] is False
    assert attrs["phoson.permission.arg_digest"] == "abc123"


def test_allowed_decision_is_ok_status() -> None:
    mw = OtelTracingMiddleware(lambda _state: None)
    _start_run(mw)
    mw.record_permission(_decision(allowed=True, level="allow", source="default"))
    state = mw._current.get()
    assert state is not None
    (span,) = [s for s in state.spans if s.name == "phoson.permission"]
    # STATUS_OK == 1 in the OTLP enum.
    assert span.status == 1


def test_run_exports_permission_count() -> None:
    collected: list[_RunState] = []
    mw = OtelTracingMiddleware(collected.append)
    _start_run(mw)
    mw.record_permission(_decision())
    mw.record_permission(_decision(tool_name="read_file", allowed=True, level="allow"))
    mw._on_done(
        AgentDoneEvent(
            result=AgentRunResult(
                final_content="done",
                history=[],
                input_messages=[],
                total_cost_usd=0.0,
                total_credits=0.0,
            )
        )
    )
    (state,) = collected
    assert state.permission_count == 2
    # The permission spans made it into the exported span list.
    assert sum(s.name == "phoson.permission" for s in state.spans) == 2


def test_plugin_forwards_to_its_middleware() -> None:
    plugin = PhosonOtelPlugin()
    middleware = plugin.get_middlewares()[0]
    assert isinstance(middleware, OtelTracingMiddleware)
    _start_run(middleware)
    plugin.record_permission(_decision())
    state = middleware._current.get()
    assert state is not None
    assert state.permission_count == 1


# ── host fan-out ─────────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.seen: list = []

    def record_permission(self, decision) -> None:
        self.seen.append(decision)


class _NoRecorder:
    """A plugin that does not implement the hook — must be skipped."""


class _BrokenRecorder:
    def record_permission(self, decision) -> None:
        raise RuntimeError("exporter down")


def test_record_permission_decision_fans_out_and_skips() -> None:
    rec = _Recorder()
    decision = _decision()
    # Must not raise on the non-supporting plugin.
    record_permission_decision([_NoRecorder(), rec], decision)
    assert rec.seen == [decision]


def test_record_permission_decision_swallows_exporter_failures() -> None:
    rec = _Recorder()
    # The broken exporter comes first; the healthy one must still receive it.
    record_permission_decision([_BrokenRecorder(), rec], _decision())
    assert len(rec.seen) == 1
