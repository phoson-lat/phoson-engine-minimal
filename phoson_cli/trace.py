"""Structured run-trace middleware for the headless one-shot path (#139).

The interactive front ends render tool activity live, but one-shot
(``phoson-cli -p "…"``, used by scripts, CI and the Harbor/Terminal-Bench
adapter) prints **only the final answer** — the agent's tool calls are
invisible. That makes a failed run impossible to debug.

The middleware emits machine-readable progress records (``start`` /
``tool_start`` / ``tool_done`` / ``step_done``). The one-shot process owner
emits exactly one terminal ``done`` or ``error`` after resource cleanup.
Records default to **stderr** so stdout carries exactly the final answer.

Enable per run with ``--trace`` (or the ``PHOSON_TRACE=1`` env var for
harnesses that cannot pass flags).
"""

import os
import sys
import json
from typing import Any, TextIO

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


class TraceWriter:
    """Best-effort JSONL writer shared by trace events and diagnostics."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._out = stream if stream is not None else sys.stderr

    def emit(self, event: str, **fields: Any) -> None:
        record = {"phoson_trace": event, **fields}
        try:
            self._out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._out.flush()
        except Exception:  # noqa: BLE001 - tracing must never break the run
            pass

    def diagnostic(
        self, message: str, *, level: str = "warning", source: str = "runtime"
    ) -> None:
        self.emit("diagnostic", level=level, source=source, message=message)

    def error(
        self,
        message: str,
        *,
        code: str = "runtime_error",
        retryable: bool = False,
    ) -> None:
        self.emit("error", message=message, code=code, retryable=retryable)

    def done(self, final: Any) -> None:
        self.emit("done", final=_clip(final))


def __getattr__(name: str) -> Any:
    # ``TraceMiddleware`` subclasses the engine's ``AgentMiddleware``, so it
    # lives in its own module: importing this one (``__main__`` does, for
    # ``--trace`` diagnostics) must not pull ``phoson_agent`` into every
    # startup, ``--help`` included.
    if name == "TraceMiddleware":
        from phoson_cli.trace_middleware import TraceMiddleware

        return TraceMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
