"""Tasks view: the session's background tasks in a picker (Ctrl+B / TUI).

Lists the tasks newest first (ones waiting for approval on top). From the
list the user can show a task's output (Enter), cancel it (``x``), or answer
a pending permission question (``a`` approve / ``d`` deny).
"""

from typing import Any, Literal, TypedDict
from dataclasses import dataclass
from collections.abc import Callable

from .theme import Theme
from .pickers import BasePicker, picker_style

TaskAction = Literal["show", "cancel", "approve", "deny"]

STATUS_ICONS = {"running": "⚙", "done": "✔", "error": "✖", "cancelled": "■"}


@dataclass
class TasksPickerResult:
    action: TaskAction | None = None
    task_id: int | None = None
    cancelled: bool = False


class _State(TypedDict):
    selected: int
    page: int


def order_tasks(tasks: list[Any]) -> list[Any]:
    """Approvals first, then running, then the rest; newest first in each."""

    def key(task: Any) -> tuple[int, int]:
        if getattr(task, "approval", None) is not None:
            rank = 0
        elif task.status == "running":
            rank = 1
        else:
            rank = 2
        return rank, -task.id

    return sorted(tasks, key=key)


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}"


def task_row(task: Any, width: int = 60) -> str:
    """One line for a task: icon, number, title, elapsed, live detail."""
    icon = (
        "⏸"
        if getattr(task, "approval", None) is not None
        else STATUS_ICONS.get(task.status, "·")
    )
    short = getattr(task, "short_title", None)
    title = str(short(width)) if callable(short) else " ".join(task.title.split())
    if len(title) > width:
        title = title[: width - 1] + "…"
    line = f"{icon} {task.label:<4} {title:<{width}} {format_elapsed(task.elapsed):>6}"
    approval = getattr(task, "approval", None)
    if approval is not None:
        line += f"  needs approval: {approval.tool} {approval.summary[:50]}"
    elif task.detail and task.status == "running":
        line += f"  {task.detail[:60]}"
    return line


def _render(
    tasks: list[Any], selected: int, page: int, page_size: int
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = [
        ("class:title", "  Background tasks\n"),
        ("class:header", "  " + "─" * 76 + "\n"),
    ]
    start = page * page_size
    for i in range(start, min(start + page_size, len(tasks))):
        style = "class:row.selected" if i == selected else "class:row"
        marker = "▸" if i == selected else " "
        lines.append((style, f"  {marker} {task_row(tasks[i], 44)}\n"))
    lines.append(("", "\n"))
    lines.append(
        (
            "class:footer",
            "  ↑/↓ navigate · Enter output · x cancel · a approve · d deny"
            " · Esc close\n",
        )
    )
    return lines


def build_tasks_picker(
    tasks: list[Any],
    page_size: int = 12,
    theme: "Theme | None" = None,
    *,
    on_done: Callable[[TasksPickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[TasksPickerResult]:
    """Build the tasks picker (embedded as a Float by the full-screen app)."""
    ordered = order_tasks(tasks)
    state: _State = {"selected": 0, "page": 0}

    picker: BasePicker[TasksPickerResult] = BasePicker(
        render=lambda: _render(ordered, state["selected"], state["page"], page_size),
        style=picker_style(theme=theme),
        initial=TasksPickerResult(cancelled=True),
        on_done=on_done,
        invalidate=invalidate,
    )

    def _finish(action: TaskAction) -> None:
        if not ordered:
            picker.done(TasksPickerResult(cancelled=True))
            return
        picker.done(
            TasksPickerResult(action=action, task_id=ordered[state["selected"]].id)
        )

    picker.bind_paged_nav(
        get_len=lambda: len(ordered),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        get_page=lambda: state["page"],
        set_page=lambda p: state.update(page=p),
        page_size=page_size,
        on_enter=lambda: _finish("show"),
        on_cancel=lambda: picker.done(TasksPickerResult(cancelled=True)),
    )
    picker.bind("x", lambda: _finish("cancel"))
    picker.bind("a", lambda: _finish("approve"))
    picker.bind("d", lambda: _finish("deny"))
    picker.bind("q", lambda: picker.done(TasksPickerResult(cancelled=True)))
    return picker


__all__ = [
    "TasksPickerResult",
    "build_tasks_picker",
    "format_elapsed",
    "order_tasks",
    "task_row",
]
