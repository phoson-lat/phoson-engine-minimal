"""Unit tests for the one-shot run trace (#139).

The trace middleware is pure (no LLM): feed it synthetic agent events and
assert the JSON lines. This pins the observable contract the Harbor /
Terminal-Bench adapter relies on to debug a headless run.
"""

import io
import json

import pytest

from phoson_cli.trace import TraceMiddleware, trace_enabled
from phoson_agent.models import (
    AgentErrorEvent,
    AgentStartEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)


def _records(events) -> list[dict]:
    """Run *events* through a TraceMiddleware bound to an in-memory stream."""
    buf = io.StringIO()
    middleware = TraceMiddleware(stream=buf)
    for event in events:
        asyncio_run(middleware.on_agent_event(event))
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_start_event_reports_model_and_limits() -> None:
    recs = _records([AgentStartEvent(model="m", max_iterations=5, tool_count=14)])
    assert recs == [
        {
            "phoson_trace": "start",
            "model": "m",
            "max_iterations": 5,
            "tools": 14,
        }
    ]


def test_tool_start_records_name_and_args() -> None:
    recs = _records(
        [
            AgentToolStartEvent(
                tool_name="write_file", args={"path": "/x", "content": "hi"}
            )
        ]
    )
    assert recs[0]["phoson_trace"] == "tool_start"
    assert recs[0]["tool"] == "write_file"
    assert recs[0]["args"] == {"path": "/x", "content": "hi"}


def test_tool_done_records_result_and_duration() -> None:
    recs = _records(
        [
            AgentToolDoneEvent(
                tool_name="write_file",
                result="Created: /x (2 bytes)",
                duration_ms=3,
            )
        ]
    )
    assert recs[0]["phoson_trace"] == "tool_done"
    assert recs[0]["result"] == "Created: /x (2 bytes)"
    assert recs[0]["duration_ms"] == 3
    assert recs[0]["error"] is None


def test_error_event_records_code() -> None:
    recs = _records(
        [AgentErrorEvent(message="boom", code="max_iterations", retryable=False)]
    )
    assert recs[0]["phoson_trace"] == "error"
    assert recs[0]["message"] == "boom"
    assert recs[0]["code"] == "max_iterations"


def test_long_result_is_clipped() -> None:
    recs = _records(
        [AgentToolDoneEvent(tool_name="bash", result="x" * 2000, duration_ms=1)]
    )
    assert recs[0]["result"].endswith("chars)")
    assert len(recs[0]["result"]) < 700


def test_trace_enabled_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_TRACE", "1")
    assert trace_enabled() is True
    for value in ("true", "YES", "on"):
        monkeypatch.setenv("PHOSON_TRACE", value)
        assert trace_enabled() is True
    for value in ("0", "false", "nope", ""):
        monkeypatch.setenv("PHOSON_TRACE", value)
        assert trace_enabled() is False
    monkeypatch.delenv("PHOSON_TRACE", raising=False)
    assert trace_enabled() is False


def test_trace_never_raises_on_write_failure() -> None:
    class _Boom:
        def write(self, *_a):
            raise OSError("disk full")

        def flush(self):
            raise OSError("disk full")

    middleware = TraceMiddleware(stream=_Boom())
    # Must swallow the write error rather than break the agent run.
    asyncio_run(middleware.on_agent_event(AgentStartEvent(model="m")))


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
