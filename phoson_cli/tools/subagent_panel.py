"""Subagent panel rendering and metrics wire format.

This module is **not** an agent tool: it is the renderer for the
sub-agent live panel and the home of the wire format used between
``phoson_cli.tools.subagent`` (which emits the metrics blob) and the
panel renderer that consumes it. Keeping both producer and consumer in
the same module prevents the previous drift where the producer used
``key=value`` separated by spaces while the parser expected ``|``.
"""

import time
from enum import Enum
from dataclasses import dataclass

from rich import box
from rich.table import Table

from phoson_cli.theme import Theme, load_theme
from phoson_cli.animations import SPINNER_FRAMES
from phoson_cli.formatting import abbr_tokens


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


def _build_running_table(
    tasks: list[str],
    frame_index: int,
    theme: Theme | None = None,
    progress: object | None = None,
) -> Table:
    """Build the live "running parallel agents" table for a spinner frame.

    With ``progress`` (E2) the Time/Tokens/Cost columns show live values
    per task; without it the panel falls back to the static "waiting" /
    "—" cells so the pre-E2 rendering is unchanged for callers that
    don't track progress.
    """
    theme = theme or load_theme()
    table = Table(
        box=box.ROUNDED,
        title="Running parallel agents",
        title_style=f"bold {theme.accent}",
        padding=(0, 1),
        show_lines=False,
    )

    table.add_column("#", style=theme.muted, width=3, justify="right")
    table.add_column("Status", style=theme.accent_soft, width=8)
    table.add_column("Task", style=theme.text)
    table.add_column("Time", style=theme.muted, width=9)
    table.add_column("Tokens", style=theme.muted, width=16)
    table.add_column("Cost", style=theme.muted, width=10)

    progress_tasks = _progress_tasks(progress)
    by_index = {p.index: p for p in (progress_tasks or [])}
    for idx, task in enumerate(tasks):
        task_preview = task[:35] + "..." if len(task) > 35 else task
        p = by_index.get(idx)
        if p is None:
            row = (
                str(idx),
                SPINNER_FRAMES[(frame_index + idx) % len(SPINNER_FRAMES)],
                task_preview,
                "waiting",
                "—",
                "—",
            )
        elif not p.started_at:
            # Registered but queued (the parallelism semaphore hasn't
            # released it yet): no clock to show, so keep "waiting".
            row = (
                str(idx),
                SPINNER_FRAMES[(frame_index + idx) % len(SPINNER_FRAMES)],
                task_preview,
                "waiting",
                _format_tokens(p.input_tokens, p.output_tokens),
                _format_cost(p.cost_usd),
            )
        else:
            # Running tasks tick against the wall clock so Time advances
            # between LLM steps (genuinely live); terminal tasks freeze
            # at their reported final duration (last_update).
            if p.status == AgentStatus.RUNNING:
                now = time.monotonic()
            else:
                now = p.last_update
            row = (
                str(idx),
                _status_cell(p, frame_index, idx),
                task_preview,
                _format_duration(p.elapsed_ms(now)),
                _format_tokens(p.input_tokens, p.output_tokens),
                _format_cost(p.cost_usd),
            )
        table.add_row(*row)
    return table


def _status_cell(p: SubagentProgress, frame_index: int, idx: int) -> str:
    """Status cell for a progress-tracked row: spinner while running."""
    if p.status == AgentStatus.ERROR:
        return "✗"
    if p.status == AgentStatus.DONE:
        return "✓"
    return SPINNER_FRAMES[(frame_index + idx) % len(SPINNER_FRAMES)]


def _format_duration(ms: int) -> str:
    """Format a duration in the smallest sensible unit (ms → s → m → h).

    Compact, space-free units so the cell stays narrow: ``450ms``, ``45s``,
    ``2m3s``, ``1h2m3s``. Sub-minute seconds keep one decimal (dropped when
    whole) so short runs don't collapse to ``0s`` and mid-second precision
    survives.
    """
    if ms < 1000:
        return f"{ms}ms"
    total_seconds = ms / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds // 60) % 60)
    seconds = total_seconds % 60
    if hours:
        return f"{hours}h{minutes}m{int(seconds)}s"
    if minutes:
        return f"{minutes}m{int(seconds)}s"
    secs = f"{seconds:.1f}".rstrip("0").rstrip(".")
    return f"{secs}s"


def _format_tokens(in_tok: int, out_tok: int) -> str:
    """Format token count, abbreviating each side (``42in / 17out`` →
    ``1.2Kin / 3.4Kout`` above 1000). Uses the shared :func:`abbr_tokens`
    so this table and the context-usage header can never diverge."""
    if in_tok == 0 and out_tok == 0:
        return "—"
    return f"{abbr_tokens(in_tok)}in / {abbr_tokens(out_tok)}out"


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
                    m.fallback_model = kv.get("fallback_model") or None
                    break

            metrics.append(m)
        except (ValueError, IndexError):
            continue
    return metrics


def render_subagent_panel(
    tasks: list[str],
    theme: Theme | None = None,
    progress: object | None = None,
) -> Table:
    """Render the initial subagent panel with pending tasks."""
    return _build_running_table(tasks, frame_index=0, theme=theme, progress=progress)


def render_subagent_panel_frame(
    tasks: list[str],
    frame_index: int,
    theme: Theme | None = None,
    progress: object | None = None,
) -> Table:
    """Render the live subagent panel for a given spinner frame."""
    return _build_running_table(tasks, frame_index, theme=theme, progress=progress)


def render_subagent_summary(
    metrics: list[SubagentMetrics], theme: Theme | None = None
) -> Table | None:
    """Render summary panel with all agent results."""
    theme = theme or load_theme()
    if not metrics:
        return None
    done_count = sum(1 for m in metrics if m.status == AgentStatus.DONE)
    table = Table(
        box=box.ROUNDED,
        title=f"{done_count}/{len(metrics)} parallel agents completed",
        title_style=f"bold {theme.accent}",
        padding=(0, 1),
        show_lines=True,
    )
    table.add_column("#", style=theme.muted, width=3, justify="right")
    table.add_column("Status", style=theme.accent_soft, width=8)
    table.add_column("Task", style=theme.text)
    table.add_column("Time", style=theme.muted, width=9)
    table.add_column("Tokens", style=theme.muted, width=16)
    table.add_column("Cost", style=theme.muted, width=10)
    total_duration, total_input, total_output, total_cost = 0, 0, 0, 0.0
    fallback_notes: list[str] = []
    for m in metrics:
        status_icon = _SUBAGENT_STATUS.get(m.status.value, "○")
        task_preview = m.task[:35] + "..." if len(m.task) > 35 else m.task
        if m.status == AgentStatus.DONE:
            total_duration += m.duration_ms
            total_input += m.input_tokens
            total_output += m.output_tokens
            total_cost += m.cost_usd
            row_style: str | None = None
            status_cell = status_icon
            if m.fallback_model:
                # Completed, but on the fallback model — surface it
                # without drowning the panel in raw provider errors.
                row_style = theme.warn
                status_cell = f"{status_icon} ↻"
                fallback_notes.append(
                    f"#{m.index} → {m.fallback_model} (configured model unavailable)"
                )
            table.add_row(
                str(m.index),
                status_cell,
                task_preview,
                _format_duration(m.duration_ms),
                _format_tokens(m.input_tokens, m.output_tokens),
                _format_cost(m.cost_usd),
                style=row_style,
            )
        elif m.status == AgentStatus.ERROR:
            table.add_row(
                str(m.index),
                status_icon,
                task_preview,
                "—",
                "—",
                m.error[:20] if m.error else "error",
                style=theme.err,
            )
    if total_duration > 0:
        table.add_row(
            "—",
            "",
            "Total",
            _format_duration(total_duration),
            _format_tokens(total_input, total_output),
            _format_cost(total_cost),
            style=f"bold {theme.accent}",
        )
    if fallback_notes:
        table.caption = "⚠ fallback: " + "; ".join(fallback_notes)
        table.caption_style = theme.warn
    return table
