"""Internal utilities and control sentinels for the agent loop.

These pieces are private to ``phoson_agent``; they are imported by
``phoson_agent.agent`` and ``phoson_agent._loop`` but are not part of
the public API. Splitting them out keeps the public modules focused on
the orchestration story.
"""

import json
import datetime
from typing import Any
from dataclasses import field, dataclass

from phoson_llm.schemas import (
    ErrorEvent,
    UsageEvent,
    LLMDoneEvent,
    ToolCallEvent,
)
from phoson_agent.models import AgentEvent, AgentErrorEvent

# ─── Time / formatting helpers ───────────────────────────────────────────────


def now_utc() -> datetime.datetime:
    """Returns the current date and time in UTC."""
    return datetime.datetime.now(datetime.UTC)


def duration_ms(started_at: datetime.datetime, ended_at: datetime.datetime) -> int:
    """Calculates the duration in milliseconds between two timestamps."""
    return int((ended_at - started_at).total_seconds() * 1000)


def to_result_text(value: str | dict[str, Any]) -> str:
    """Converts a tool result to a text string suitable for tool_result blocks."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True)


def subagent_label(tool_name: str) -> str | None:
    """Returns the UI label for subagent-like tools, or None.

    The CLI renderer keys off this label to switch to the live panel
    layout when ``agents`` is invoked, instead of the standard tool spinner.
    """
    if tool_name == "agent":
        return "subagent"
    if tool_name == "agents":
        return "subagents"
    return None


# ─── LLM step bookkeeping ────────────────────────────────────────────────────


@dataclass
class LLMStepOutcome:
    """Aggregated output of consuming a single LLM stream iteration.

    The agent loop demultiplexes the typed ``LLMEvent`` stream into this
    bag so that the post-stream step (cost accounting, tool dispatch,
    history bookkeeping) doesn't need to traverse the events twice.
    """

    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    usage_event: UsageEvent | None = None
    done_event: LLMDoneEvent | None = None
    error_event: ErrorEvent | None = None


# ─── Internal control sentinels for the iteration generator ──────────────────
#
# These are yielded by ``_AgentLoop._run_iteration`` to communicate flow
# control back to ``AgentEngine._stream_impl`` without conflating them with
# the public ``AgentEvent`` stream the consumer sees. They inherit from
# ``AgentEvent`` purely so the generator's annotated yield type stays
# uniform; they are stripped before the public stream is yielded.


@dataclass(kw_only=True)
class IterationCost(AgentEvent):
    """Internal: signals incremental cost from one LLM call."""

    cost_usd: float = 0.0
    credits: float = 0.0


@dataclass(kw_only=True)
class IterationFinal(AgentEvent):
    """Internal: signals the iteration produced a final assistant answer."""

    final_content: str = ""


@dataclass(kw_only=True)
class IterationFailed(AgentEvent):
    """Internal: signals the iteration failed, carrying the public error."""

    error_event: AgentErrorEvent = field(default_factory=AgentErrorEvent)
