"""Subagent panel tools for managing and rendering parallel agent tasks."""

from enum import Enum
from typing import Any
from dataclasses import dataclass

from rich import box
from rich.table import Table

from .base import BaseTool


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


class SubagentPanelTool(BaseTool):
    """Tool to manage subagent panel rendering."""

    def run(self, *args: Any, **kwargs: Any) -> Any:
        # Panel logic is primarily UI-driven
        pass

    def render_panel(self, tasks: list[str]) -> Table:
        """Render initial subagent panel with pending tasks."""
        return self.render_panel_frame(tasks, 0)

    def render_panel_frame(self, tasks: list[str], frame_index: int) -> Table:
        """Render live subagent panel for a spinner frame."""
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


def parse_subagent_metrics(output: str) -> list[SubagentMetrics]:
    """Parse metrics from subagent output string."""
    metrics: list[SubagentMetrics] = []
    parts = output.split("=== Agent ")
    for part in parts[1:]:
        try:
            lines = part.split("\n")
            if not lines:
                continue
            header_line = lines[0]
            idx = int(header_line.split(":")[0])
            task = header_line.split(":", 1)[1].replace("===", "").strip()
            metrics_line = ""
            for line in lines:
                if "--- METRICS:" in line:
                    metrics_line = line
                    break
            m = SubagentMetrics(index=idx, task=task, status=AgentStatus.DONE)
            if "Error:" in header_line:
                m.status = AgentStatus.ERROR
                m.error = header_line.split("Error:")[1].strip()
            elif metrics_line:
                parts2 = metrics_line.replace("--- METRICS:", "").replace("---", "").split("|")
                if len(parts2) >= 3:
                    m.duration_ms = int(parts2[0].strip().replace("ms", ""))
                    tok_str = parts2[1].strip()
                    if "in/" in tok_str and tok_str.endswith("out"):
                        in_tok, out_tok = tok_str[:-3].split("in/")
                        m.input_tokens = int(in_tok)
                        m.output_tokens = int(out_tok)
                    cost_str = parts2[2].strip()
                    if cost_str.startswith("$"):
                        m.cost_usd = float(cost_str.replace("$", ""))
            metrics.append(m)
        except (ValueError, IndexError):
            continue
    return metrics


def render_subagent_panel(tasks: list[str]) -> Table:
    """Render initial subagent panel with pending tasks.
    
    Args:
        tasks: List of task descriptions to display.
        
    Returns:
        A Rich Table representing the initial panel state.
    """
    panel_tool = SubagentPanelTool()
    return panel_tool.render_panel(tasks)


def render_subagent_panel_frame(tasks: list[str], frame_index: int) -> Table:
    """Render live subagent panel for a spinner frame.
    
    Args:
        tasks: List of task descriptions.
        frame_index: Current frame index for spinner animation.
        
    Returns:
        A Rich Table with animated spinner.
    """
    panel_tool = SubagentPanelTool()
    return panel_tool.render_panel_frame(tasks, frame_index)


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
            table.add_row(str(m.index), status_icon, task_preview, _format_duration(m.duration_ms), _format_tokens(m.input_tokens, m.output_tokens), _format_cost(m.cost_usd))
        elif m.status == AgentStatus.ERROR:
            table.add_row(str(m.index), status_icon, task_preview, "—", "—", m.error[:20] if m.error else "error", style=_TOOL_ERR)
    if total_duration > 0:
        table.add_row("—", "", "Total", _format_duration(total_duration), _format_tokens(total_input, total_output), _format_cost(total_cost), style=f"bold {_ACCENT}")
    return table
