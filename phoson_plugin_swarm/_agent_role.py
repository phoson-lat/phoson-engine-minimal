"""Agent roles, runtime instances and results for the swarm plugin.

This module is the *data core* of :mod:`phoson_plugin_swarm` and owns
:class:`SwarmError` (imported by the other plugin modules, so it lives here
rather than in a dedicated error file). It has no dependencies on the engine
beyond plain dataclasses, which keeps it trivially importable from tests.

A swarm is a group of *specialized* :class:`AgentRole` configurations that
the orchestrator turns into :class:`AgentInstance` objects at run time. Each
instance owns its own :class:`phoson_agent.agent.AgentEngine` (so every member
gets a small, focused context instead of one agent with a giant one — the core
motivation for the plugin) and reports back an :class:`AgentResult`.
"""

import time
from typing import Any
from dataclasses import field, dataclass

#: Tool names a swarm member must never be offered: the delegation tools
#: (one-level recursion bound, matching the CLI sub-agent design) and the
#: swarm tools themselves (so a member cannot spawn a nested swarm).
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "agents",
        "swarm_create",
        "swarm_assign",
        "swarm_message",
        "swarm_status",
        "swarm_collect",
        "swarm_dissolve",
    }
)


class SwarmError(Exception):
    """Raised for user-actionable swarm configuration or runtime errors."""


# ── Lifecycle status values ─────────────────────────────────────────────────
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_BUDGET = "budget_exhausted"
STATUS_DISSOLVED = "dissolved"


def _as_str_list(value: Any, field_name: str, agent: str) -> list[str] | None:
    """Validate an optional list-of-strings role field (``tools_allowlist``)."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise SwarmError(
            f"agent role {agent!r} {field_name!r} must be a list of tool names"
        )
    return list(value)


@dataclass
class AgentRole:
    """Declarative description of one specialized swarm member.

    Mirrors the issue's proposed shape. ``model`` overrides the swarm's
    default model (e.g. a cheaper model for a simple role). ``tools_allowlist``
    restricts which tools this member may call — ``None`` means "everything the
    swarm's host offers except the reserved delegation/swarm tools". A member
    can *never* call a tool outside its allowlist (DoD).
    """

    name: str
    system_prompt: str
    model: str | None = None
    tools_allowlist: list[str] | None = None
    max_tokens: int | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "AgentRole":
        """Build a role from the LLM-facing dict form (see ``swarm_create``)."""
        if not isinstance(raw, dict):
            raise SwarmError(
                "each agent in swarm_create must be an object with at least "
                "'name' and 'system_prompt'"
            )
        name = raw.get("name")
        system_prompt = raw.get("system_prompt")
        if not isinstance(name, str) or not name.strip():
            raise SwarmError("each agent needs a non-empty string 'name'")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise SwarmError(f"agent {name!r} needs a non-empty 'system_prompt'")

        model = raw.get("model")
        if model is not None and not isinstance(model, str):
            raise SwarmError(f"agent {name!r} 'model' must be a string or null")

        max_tokens = raw.get("max_tokens")
        if max_tokens is not None:
            if (
                not isinstance(max_tokens, int)
                or isinstance(max_tokens, bool)
                or max_tokens <= 0
            ):
                raise SwarmError(
                    f"agent {name!r} 'max_tokens' must be a positive integer"
                )

        return cls(
            name=name.strip(),
            system_prompt=system_prompt,
            model=model or None,
            tools_allowlist=_as_str_list(
                raw.get("tools_allowlist"), "tools_allowlist", name
            ),
            max_tokens=max_tokens,
        )


@dataclass
class AgentResult:
    """The outcome of one agent's run (fed into :meth:`AgentInstance.status`)."""

    agent: str
    status: str = STATUS_DONE
    content: str = ""
    task: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    @property
    def tokens_used(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "task": self.task,
            "content": self.content,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
        }


@dataclass
class AgentInstance:
    """Runtime wrapper around a role: current task, status and last result.

    The orchestrator (re)uses the same instance across ``swarm_assign`` calls;
    only its mutable fields change.
    """

    role: AgentRole
    task: str | None = None
    status: str = STATUS_IDLE
    result: AgentResult | None = None

    @property
    def name(self) -> str:
        return self.role.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.role.name,
            "model": self.role.model,
            "tools": (
                self.role.tools_allowlist
                if self.role.tools_allowlist is not None
                else "all"
            ),
            "max_tokens": self.role.max_tokens,
            "status": self.status,
            "task": self.task,
            "result": self.result.to_dict() if self.result is not None else None,
        }


def make_agent_result(
    agent: str,
    *,
    status: str = STATUS_DONE,
    content: str = "",
    task: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error: str | None = None,
    started_at: float | None = None,
) -> AgentResult:
    """Convenience constructor that fills ``started_at`` when not given."""
    return AgentResult(
        agent=agent,
        status=status,
        content=content,
        task=task,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        error=error,
        started_at=started_at if started_at is not None else time.monotonic(),
    )
