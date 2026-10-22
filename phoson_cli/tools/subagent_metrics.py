"""Sub-agent metrics wire format and live progress data (UI-free).

Producer and consumer of the metrics blob live together here so they
cannot drift (the producer once used ``key=value`` separated by spaces
while the parser expected ``|``). The producer is
``phoson_cli.tools.subagent``; the Rich panels that render these values
live in :mod:`phoson_cli.tools.subagent_panel`.

This module must stay free of ``rich``/``prompt_toolkit`` so the agent
tools (and the session runtime that registers them) import without the
TUI stack.
"""

from enum import Enum
from dataclasses import dataclass


class AgentStatus(Enum):
    """Represent the execution state of a subagent."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


_SUBAGENT_STATUS = {
    "pending": "○",
    "running": "◐",
    "done": "✓",
    "error": "✗",
}

# ─── Wire format ──────────────────────────────────────────────────────────────
# Producer: ``phoson_cli.tools.subagent.agents``.
# Consumer: ``parse_subagent_metrics`` below.
# Format (one line):
#   --- METRICS: k1=v1 k2=v2 ... ---
# Keys are space-separated ``key=value`` pairs. Values must not contain
# whitespace; numeric values are emitted in canonical Python repr.

AGENT_HEADER_PREFIX = "=== Agent "
AGENT_HEADER_SUFFIX = " ==="
METRICS_PREFIX = "--- METRICS:"
METRICS_SUFFIX = "---"


def format_metrics_line(
    *,
    duration_ms: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    credits: float | int = 0,
    fallback_model: str | None = None,
) -> str:
    """Build the canonical metrics line consumed by ``parse_subagent_metrics``.

    ``fallback_model`` (optional) records that this sub-agent had to run
    on the fallback model instead of the configured one — the summary
    panel renders a visible warning for it.
    """
    line = (
        f"{METRICS_PREFIX} "
        f"duration_ms={duration_ms} "
        f"input_tokens={input_tokens} "
        f"output_tokens={output_tokens} "
        f"cost_usd={cost_usd} "
        f"credits={credits} "
    )
    if fallback_model:
        # Model ids contain no whitespace, safe for the k=v wire format.
        line += f"fallback_model={fallback_model} "
    return f"{line}{METRICS_SUFFIX}"


def format_agent_block(
    *,
    index: int,
    task_preview: str,
    body: str,
    metrics_line: str | None = None,
    error: str | None = None,
) -> str:
    """Build a full ``=== Agent N: <preview> === ...`` block.

    Mirrors the producer in ``phoson_cli.tools.subagent``.
    """
    header = f"{AGENT_HEADER_PREFIX}{index}: {task_preview}{AGENT_HEADER_SUFFIX}"
    if error is not None:
        return f"{header} Error: {error}"
    if metrics_line:
        return f"{header}\n{body}\n{metrics_line}"
    return f"{header}\n{body}"


@dataclass
class SubagentMetrics:
    """Metrics for a single subagent task."""

    index: int
    task: str
    status: AgentStatus
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    fallback_model: str | None = None


@dataclass
class SubagentProgress:
    """Live metrics for one in-flight sub-agent task (E2).

    Values are best-effort snapshots: tokens/cost only become available
    once a sub-agent's first LLM call finishes, so rows stay "—" until
    then. ``status`` mirrors the live phase the panel should show.
    """

    index: int
    task: str
    status: AgentStatus = AgentStatus.RUNNING
    started_at: float = 0.0
    last_update: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    done: bool = False

    @property
    def has_tokens(self) -> bool:
        """True once at least one LLM call reported usage."""
        return self.input_tokens > 0 or self.output_tokens > 0

    def elapsed_ms(self, now: float) -> int:
        """Milliseconds since start (0 when ``started_at`` is unset)."""
        if not self.started_at:
            return 0
        reference = now if now > self.started_at else self.started_at
        return int((reference - self.started_at) * 1000)


def _progress_tasks(progress: object | None) -> list[SubagentProgress] | None:
    """Normalize a progress source to a list of per-task progress.

    Accepts either the producer-side ``SubagentProgressTracker`` (which
    exposes a ``tasks`` list) or a plain ``list[SubagentProgress]`` —
    both are equally valid for rendering.
    """
    if progress is None:
        return None
    tasks = getattr(progress, "tasks", None)
    if isinstance(tasks, list):
        return tasks
    if isinstance(progress, list):
        return progress
    return None


def _parse_kv_pairs(line: str) -> dict[str, str]:
    """Parse ``k1=v1 k2=v2 ...`` into a dict. Values without ``=`` are skipped."""
    out: dict[str, str] = {}
    for token in line.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key.strip()] = value.strip()
    return out


def parse_subagent_metrics(output: str) -> list[SubagentMetrics]:
    """Parse metrics from a subagent output string.

    The producer is :func:`phoson_cli.tools.subagent.agents` (via
    :func:`format_agent_block`/:func:`format_metrics_line`). Both ends
    live in this module to keep the wire format honest.
    """
    metrics: list[SubagentMetrics] = []
    parts = output.split(AGENT_HEADER_PREFIX)
    for part in parts[1:]:
        try:
            lines = part.split("\n")
            if not lines:
                continue
            header_line = lines[0]
            # Header: ``N: <preview> === [Error: ...]``
            idx_str, _, rest = header_line.partition(":")
            idx = int(idx_str.strip())
            task_segment, _, after = rest.partition(AGENT_HEADER_SUFFIX)
            task = task_segment.strip()

            m = SubagentMetrics(index=idx, task=task, status=AgentStatus.DONE)

            if "Error:" in after:
                m.status = AgentStatus.ERROR
                m.error = after.split("Error:", 1)[1].strip()
                metrics.append(m)
                continue

            for line in lines[1:]:
                if METRICS_PREFIX in line:
                    payload = (
                        line.split(METRICS_PREFIX, 1)[1]
                        .rsplit(METRICS_SUFFIX, 1)[0]
                        .strip()
                    )
                    kv = _parse_kv_pairs(payload)
                    m.duration_ms = int(float(kv.get("duration_ms", "0")))
                    m.input_tokens = int(float(kv.get("input_tokens", "0")))
                    m.output_tokens = int(float(kv.get("output_tokens", "0")))
                    m.cost_usd = float(kv.get("cost_usd", "0") or 0)
                    m.fallback_model = kv.get("fallback_model") or None
                    break

            metrics.append(m)
        except (ValueError, IndexError):
            continue
    return metrics
