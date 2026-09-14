"""Structured run-trace middleware for the headless one-shot path (#139).

The interactive front ends render tool activity live, but one-shot
(``phoson-cli -p "…"``, used by scripts, CI and the Harbor/Terminal-Bench
adapter) prints **only the final answer** — the agent's tool calls are
invisible. That makes a failed run impossible to debug.

This middleware emits one machine-readable JSON line per agent event
(``start`` / ``tool_start`` / ``tool_done`` / ``step_done`` / ``done`` /
``error``) to a stream. It defaults to **stderr** so stdout keeps carrying
exactly the final answer (external harnesses capture stdout).

Enable per run with ``--trace`` (or the ``PHOSON_TRACE=1`` env var for
harnesses that cannot pass flags).
"""

import os
import sys
import json
from typing import Any, TextIO

from phoson_agent.models import (
    AgentEvent,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_agent.middleware import AgentMiddleware

#: Max characters kept from a tool result / step / final answer in one line.
_CLIP_CHARS = 600


def trace_enabled() -> bool:
    """Whether the run trace should be on (``--trace`` or ``PHOSON_TRACE``)."""
    value = os.environ.get("PHOSON_TRACE", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _clip(value: Any, limit: int = _CLIP_CHARS) -> str:
    """Stringify *value* and truncate it to *limit* characters."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… (+{len(text) - limit} chars)"


class TraceMiddleware(AgentMiddleware):
    """Emit one JSON line per agent event for a headless run.

    Only implements :meth:`on_agent_event`, so its position in the
    middleware chain is inert for gating (offload/summarizer/permission).
    Writing never raises: a tracing failure must not break the run.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._out = stream if stream is not None else sys.stderr

    def _emit(self, event: str, **fields: Any) -> None:
        record = {"phoson_trace": event, **fields}
        try:
            self._out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._out.flush()
        except Exception:  # noqa: BLE001 — tracing must never break the run
            pass

    async def on_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self._emit(
                "start",
                model=event.model,
                max_iterations=event.max_iterations,
                tools=event.tool_count,
            )
        elif isinstance(event, AgentToolStartEvent):
            self._emit("tool_start", tool=event.tool_name, args=event.args)
        elif isinstance(event, AgentToolDoneEvent):
            self._emit(
                "tool_done",
                tool=event.tool_name,
                error=event.error,
                duration_ms=event.duration_ms,
                result=_clip(event.result),
            )
        elif isinstance(event, AgentStepDoneEvent):
            step = event.step
            self._emit(
                "step_done",
                kind=getattr(step, "kind", None),
                tool=getattr(step, "tool_name", None),
                duration_ms=getattr(step, "duration_ms", None),
            )
        elif isinstance(event, AgentDoneEvent):
            self._emit(
                "done",
                final=_clip(getattr(event.result, "final_content", None)),
            )
        elif isinstance(event, AgentErrorEvent):
            self._emit(
                "error",
                message=event.message,
                code=event.code,
                retryable=event.retryable,
            )
