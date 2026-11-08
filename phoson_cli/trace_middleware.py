"""Agent middleware that streams a headless run as JSONL trace events."""

import os
import re
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

#: Environment variables whose values are secrets (API keys, tokens, …).
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
#: Well-known credential shapes, redacted even when not in the environment.
_SECRET_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ci_live_[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"
)
_REDACTED = "[REDACTED]"


def _secret_values() -> list[str]:
    """Secret values from the environment, longest first (min. 8 chars)."""
    values = {
        value
        for name, value in os.environ.items()
        if _SECRET_ENV.search(name) and len(value) >= 8
    }
    return sorted(values, key=len, reverse=True)


def redact(value: Any, secrets: list[str]) -> Any:
    """Replace secret values and credential-shaped strings, recursively."""
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, _REDACTED)
        return _SECRET_SHAPES.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    return value


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
        # Traces usually land in CI logs: tool arguments and results are
        # scrubbed of API keys and tokens before they are written.
        self._secrets = _secret_values()

    def _emit(self, event: str, **fields: Any) -> None:
        self._writer.emit(event, **redact(fields, self._secrets))

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
