"""Per-phase reasoning effort scheduling (the "reasoning sandwich").

Rationale (issue #145)
----------------------
Reasoning effort is not equally valuable across every phase of a single
agent run.  Planning (the first LLM step) and verification (the step
immediately after a tool failure) benefit from maximum thinking;
executing a mechanical command does not.  The pattern follows LangChain's
recommendation: concentrate thinking in planning and verification, keep it
minimal in execution.

This module is deliberately pure: every function here is deterministic,
has no I/O, and is independently unit-testable.  The "sandwich" is a
falsifiable hypothesis — if the #139 nightly bench shows no rate or cost
improvement, the entire module can be deleted without touching the rest of
the codebase.

Design constraints
------------------
* **Global override wins.**  If the user set ``/reasoning-effort``
  explicitly (``config.reasoning_effort`` is not ``None``), the profile is
  never applied and the override is used for every iteration.
* **Opt-in.**  When ``reasoning_sandwich`` is ``False`` (the default), no
  scheduler is installed and the behaviour is identical to today.
* **Per-adapter safety.**  The effort is passed per-request via
  ``ModelConfig.reasoning_effort``.  Adapters that do not support changing
  the thinking mode mid-run simply ignore per-request effort changes;
  those that do (OpenAI-compatible ``reasoning_effort``) honour them.
"""

from typing import Final, Literal
from collections.abc import Callable

# ── Phase types ──────────────────────────────────────────────────────────────

ReasoningPhase = Literal["planning", "execution", "verification"]

_PHASES: Final = ("planning", "execution", "verification")


def _validate_effort(effort: str | None) -> str | None:
    """Return *effort* if it is a valid reasoning-effort level, else ``None``.

    A ``None`` or unrecognised value is normalised to ``None`` so that a
    malformed profile entry degrades to "no effort" instead of raising.
    """
    if effort is None:
        return None
    # Import here to avoid a circular import at module load time.
    from phoson_llm.schemas import REASONING_EFFORTS  # noqa: PLC0415

    return effort if effort in REASONING_EFFORTS else None


# ── Default profile ──────────────────────────────────────────────────────────

#: Conservative default profile for the reasoning sandwich (#145).
#: Planning and verification get the highest effort; execution gets the
#: lowest.  Values are validated against ``REASONING_EFFORTS`` at build time.
DEFAULT_PHASE_PROFILE: Final[dict[str, str]] = {
    "planning": "high",
    "execution": "low",
    "verification": "high",
}

# ── Phase detection ──────────────────────────────────────────────────────────


def detect_phase(iteration_index: int, last_tool_error: bool) -> ReasoningPhase:
    """Determine the reasoning phase for a given iteration.

    Args:
        iteration_index: Zero-based index of the current ReAct iteration.
        last_tool_error: ``True`` if the *previous* iteration ended with at
            least one tool call whose result carried an error.

    Returns:
        ``"planning"`` for the first iteration, ``"verification"`` for the
        iteration immediately after a tool failure (index > 0), and
        ``"execution"`` otherwise.

    Rules (simple, no classifier):
        * index 0 → **planning**
        * index > 0 and ``last_tool_error`` → **verification**
        * otherwise → **execution**
    """
    if iteration_index == 0:
        return "planning"
    if last_tool_error:
        return "verification"
    return "execution"


# ── Effort resolution ─────────────────────────────────────────────────────────


def resolve_phase_effort(
    phase: ReasoningPhase,
    profile: dict[str, str] | None = None,
    override: str | None = None,
) -> str | None:
    """Resolve the reasoning effort for *phase*, honouring the global override.

    Args:
        phase: The detected reasoning phase.
        profile: Per-phase effort mapping.  Defaults to
            :data:`DEFAULT_PHASE_PROFILE`.  Entries that are not a valid
            effort level are normalised to ``None``.
        override: The user's explicit ``/reasoning-effort`` value (or
            ``PHOSON_REASONING_EFFORT`` / config.toml).  When not ``None``
            it takes precedence over the profile for *all* phases.

    Returns:
        The resolved effort string, or ``None`` when no effort should be
        applied (e.g. the profile entry is invalid or no override is set
        and the phase is absent from the profile).
    """
    # Global override always wins.
    if override is not None:
        return _validate_effort(override)

    effective_profile = profile if profile is not None else DEFAULT_PHASE_PROFILE
    return _validate_effort(effective_profile.get(phase))


# ── Scheduler (callable handed to AgentEngine) ────────────────────────────────

#: Type alias for the callable injected into :class:`AgentEngine`.
#: Signature: ``(iteration_index, last_tool_error) -> effort | None``.
EffortScheduler = Callable[[int, bool], str | None]


def build_effort_scheduler(
    profile: dict[str, str] | None = None,
    override: str | None = None,
) -> EffortScheduler:
    """Build an :data:`EffortScheduler` closure for the given profile.

    The returned callable has the signature required by
    ``AgentEngine.effort_scheduler``:
    ``(iteration_index: int, last_tool_error: bool) -> str | None``.

    Args:
        profile: Per-phase effort mapping (``None`` → default).
        override: The user's explicit global effort (``None`` → use the
            profile).  When set, the scheduler ignores the profile entirely
            and returns the override for every iteration.

    Returns:
        A pure, stateless callable suitable for ``AgentEngine``.
    """
    effective_profile = profile if profile is not None else DEFAULT_PHASE_PROFILE

    def _scheduler(iteration_index: int, last_tool_error: bool) -> str | None:
        return resolve_phase_effort(
            phase=detect_phase(iteration_index, last_tool_error),
            profile=effective_profile,
            override=override,
        )

    return _scheduler


def make_live_scheduler(
    get_override: Callable[[], str | None],
    get_profile: Callable[[], dict[str, str] | None] | None = None,
) -> EffortScheduler:
    """Build an :data:`EffortScheduler` that reads its inputs *lazily*.

    Unlike :func:`build_effort_scheduler`, which captures a fixed profile and
    override at construction time, this factory returns a scheduler that calls
    ``get_override()`` and ``get_profile()`` on **every iteration**.  This is
    what the CLI uses so that a mid-session ``/reasoning-effort`` (which
    mutates ``config.reasoning_effort``) still wins over the per-phase profile
    on the very next iteration, without rebuilding the scheduler.

    Args:
        get_override: Zero-arg callable returning the current global effort
            override (``None`` when the user has not set one).
        get_profile: Zero-arg callable returning the current per-phase
            profile, or ``None`` to use the default.  May itself be ``None``
            (fixed to always use the default profile).

    Returns:
        A pure, stateless callable suitable for ``AgentEngine``.
    """

    def _scheduler(iteration_index: int, last_tool_error: bool) -> str | None:
        return resolve_phase_effort(
            phase=detect_phase(iteration_index, last_tool_error),
            profile=get_profile() if get_profile is not None else None,
            override=get_override(),
        )

    return _scheduler
