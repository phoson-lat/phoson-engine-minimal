"""Subagent panel rendering and metrics wire format.

This module is **not** an agent tool: it is the renderer for the
sub-agent live panel and the home of the wire format used between
``phoson_cli.tools.subagent`` (which emits the metrics blob) and the
panel renderer that consumes it. Keeping both producer and consumer in
the same module prevents the previous drift where the producer used
``key=value`` separated by spaces while the parser expected ``|``.
"""

from enum import Enum
from dataclasses import dataclass

from rich import box
from rich.table import Table


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

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_ACCENT = "medium_purple1"
_ACCENT2 = "plum3"
_MUTED = "grey50"
_TOOL_OK = "medium_spring_green"
_TOOL_ERR = "indian_red1"

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
) -> str:
    """Build the canonical metrics line consumed by ``parse_subagent_metrics``."""
    return (
        f"{METRICS_PREFIX} "
        f"duration_ms={duration_ms} "
        f"input_tokens={input_tokens} "
        f"output_tokens={output_tokens} "
        f"cost_usd={cost_usd} "
        f"credits={credits} "
        f"{METRICS_SUFFIX}"
    )


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


def _build_running_table(tasks: list[str], frame_index: int) -> Table:
    """Build the live "running parallel agents" table for a spinner frame."""
    table = Table(
        box=box.ROUNDED,
        title="Running parallel agents",
        title_style=f"bold {_ACCENT}",
        padding=(0, 1),
        show_lines=False,
    )

    table.add_column("#", style=_MUTED, width=3, justify="right")
    table.add_column("Status", style=_ACCENT2, width=8)
    table.add_column("Task", style="white")
    table.add_column("Time", style=_MUTED, width=8)
    table.add_column("Tokens", style=_MUTED, width=14)
    table.add_column("Cost", style=_MUTED, width=10)

    for idx, task in enumerate(tasks):
        task_preview = task[:35] + "..." if len(task) > 35 else task
        table.add_row(
            str(idx),
            _SPINNER_FRAMES[(frame_index + idx) % len(_SPINNER_FRAMES)],
            task_preview,
            "waiting",
            "—",
            "—",
        )
    return table


def _format_duration(ms: int) -> str:
    """Format duration in human-readable form."""
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _format_tokens(in_tok: int, out_tok: int) -> str:
    """Format token count."""
    if in_tok == 0 and out_tok == 0:
        return "—"
    return f"{in_tok}in / {out_tok}out"


def _format_cost(cost: float) -> str:
    """Format cost in USD."""
    if cost == 0:
        return "—"
    return f"${cost:.5f}"


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
                    break

            metrics.append(m)
        except (ValueError, IndexError):
            continue
    return metrics


def render_subagent_panel(tasks: list[str]) -> Table:
    """Render the initial subagent panel with pending tasks."""
    return _build_running_table(tasks, frame_index=0)


def render_subagent_panel_frame(tasks: list[str], frame_index: int) -> Table:
    """Render the live subagent panel for a given spinner frame."""
    return _build_running_table(tasks, frame_index)


def render_subagent_summary(metrics: list[SubagentMetrics]) -> Table | None:
    """Render summary panel with all agent results."""
    if not metrics:
        return None
    done_count = sum(1 for m in metrics if m.status == AgentStatus.DONE)
    table = Table(
        box=box.ROUNDED,
        title=f"{done_count}/{len(metrics)} parallel agents completed",
        title_style=f"bold {_ACCENT}",
        padding=(0, 1),
        show_lines=True,
    )
    table.add_column("#", style=_MUTED, width=3, justify="right")
    table.add_column("Status", style=_ACCENT2, width=8)
    table.add_column("Task", style="white")
    table.add_column("Time", style=_MUTED, width=8)
    table.add_column("Tokens", style=_MUTED, width=14)
    table.add_column("Cost", style=_MUTED, width=10)
    total_duration, total_input, total_output, total_cost = 0, 0, 0, 0.0
    for m in metrics:
        status_icon = _SUBAGENT_STATUS.get(m.status.value, "○")
        task_preview = m.task[:35] + "..." if len(m.task) > 35 else m.task
        if m.status == AgentStatus.DONE:
            total_duration += m.duration_ms
            total_input += m.input_tokens
            total_output += m.output_tokens
            total_cost += m.cost_usd
            table.add_row(
                str(m.index),
                status_icon,
                task_preview,
                _format_duration(m.duration_ms),
                _format_tokens(m.input_tokens, m.output_tokens),
                _format_cost(m.cost_usd),
            )
        elif m.status == AgentStatus.ERROR:
            table.add_row(
                str(m.index),
                status_icon,
                task_preview,
                "—",
                "—",
                m.error[:20] if m.error else "error",
                style=_TOOL_ERR,
            )
    if total_duration > 0:
        table.add_row(
            "—",
            "",
            "Total",
            _format_duration(total_duration),
            _format_tokens(total_input, total_output),
            _format_cost(total_cost),
            style=f"bold {_ACCENT}",
        )
    return table
