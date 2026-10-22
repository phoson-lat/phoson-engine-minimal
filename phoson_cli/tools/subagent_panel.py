"""Rich rendering for the sub-agent live and summary panels.

This module is **not** an agent tool. The metrics wire format and the
live progress data it renders live in
:mod:`phoson_cli.tools.subagent_metrics` (UI-free, shared with the
producer in ``phoson_cli.tools.subagent``); they are re-exported here for
existing importers.
"""

import time

from rich import box
from rich.table import Table

from phoson_cli.theme import Theme, load_theme
from phoson_cli.animations import SPINNER_FRAMES
from phoson_cli.formatting import abbr_tokens

from .subagent_metrics import (  # noqa: F401 - re-exported
    METRICS_PREFIX,
    METRICS_SUFFIX,
    _SUBAGENT_STATUS,
    AGENT_HEADER_PREFIX,
    AGENT_HEADER_SUFFIX,
    AgentStatus,
    SubagentMetrics,
    SubagentProgress,
    _progress_tasks,
    format_agent_block,
    format_metrics_line,
    parse_subagent_metrics,
)


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
