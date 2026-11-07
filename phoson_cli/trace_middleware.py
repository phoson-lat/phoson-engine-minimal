"""Agent middleware that streams a headless run as JSONL trace events."""

from typing import Any, TextIO

from phoson_cli.trace import TraceWriter, _clip
from phoson_agent.models import (
    AgentEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_agent.middleware import AgentMiddleware


class TraceMiddleware(AgentMiddleware):
    """Emit one JSON line per agent event for a headless run.

    Only implements :meth:`on_agent_event`, so its position in the
    middleware chain is inert for gating (offload/summarizer/permission).
    Writing never raises: a tracing failure must not break the run.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        writer: TraceWriter | None = None,
    ) -> None:
        self._writer = writer or TraceWriter(stream)
        self.terminal_error: AgentErrorEvent | None = None

    def _emit(self, event: str, **fields: Any) -> None:
        self._writer.emit(event, **fields)

    async def on_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self.terminal_error = None
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
        elif isinstance(event, AgentErrorEvent):
            self.terminal_error = event
