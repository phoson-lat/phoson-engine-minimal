"""Model-facing tools for the session's background tasks (``#3``, …).

``task_status`` lists them, ``task_wait`` blocks until some finish,
``task_output`` returns one task's full result (or live progress), and
``task_cancel`` stops one. They are main-engine-only tools: sub-agents never
see them, and without a task manager (one-shot mode) they say so.
"""

from typing import Annotated

from phoson_agent.tool import tool

from ..tasks import TaskManager, BackgroundTask

_INJECT = ["task_manager"]
_NO_MANAGER = "Background tasks are not available in this mode."

#: Upper bound for one ``task_wait`` call, so a turn never hangs forever.
MAX_WAIT_SECONDS = 1800.0


def _progress_line(task: BackgroundTask) -> str:
    progress = task.progress
    bits: list[str] = []
    if progress is not None:
        tokens = int(getattr(progress, "input_tokens", 0) or 0) + int(
            getattr(progress, "output_tokens", 0) or 0
        )
        if tokens:
            bits.append(f"{tokens:,} tokens")
        cost = float(getattr(progress, "cost_usd", 0.0) or 0.0)
        if cost:
            bits.append(f"${cost:.4f}")
    if task.detail:
        bits.append(task.detail)
    return " · ".join(bits)


def _ids(value: list[int] | None) -> list[int] | None:
    if not value:
        return None
    return [int(v) for v in value]


@tool(inject=_INJECT)
async def task_status(
    ids: Annotated[
        list[int] | None, "Task numbers to report (e.g. [3, 4]); omit for all."
    ] = None,
    *,
    task_manager: TaskManager | None = None,
) -> str:
    """List background tasks with their state, elapsed time and progress."""
    if task_manager is None:
        return _NO_MANAGER
    wanted = _ids(ids)
    tasks = [
        task for task in task_manager.tasks() if wanted is None or task.id in wanted
    ]
    if not tasks:
        return "No background tasks."
    lines = []
    for task in tasks:
        line = task.summary_line()
        extra = _progress_line(task)
        if extra:
            line += f" — {extra}"
        if task.done and not task.delivered:
            line += " (result not read yet: task_output)"
        lines.append(line)
    return "\n".join(lines)


@tool(inject=_INJECT)
async def task_wait(
    ids: Annotated[
        list[int] | None,
        "Task numbers to wait for; omit to wait for every running task.",
    ] = None,
    timeout: Annotated[
        float, "Maximum seconds to wait (default 300, max 1800)."
    ] = 300.0,
    *,
    task_manager: TaskManager | None = None,
) -> str:
    """Wait until background tasks finish and return their results.

    Only call this when you actually need the results to continue: finished
    tasks are delivered to you automatically anyway.
    """
    if task_manager is None:
        return _NO_MANAGER
    try:
        limit = float(timeout)
    except (TypeError, ValueError):
        limit = 300.0
    limit = min(max(limit, 0.0), MAX_WAIT_SECONDS)
    targets = await task_manager.wait(_ids(ids), timeout=limit)
    if not targets:
        return "No matching background tasks."
    parts = []
    for task in targets:
        if task.done:
            task.delivered = True
            parts.append(f"## {task.summary_line()}\n{task.outcome_text()}")
        else:
            parts.append(f"## {task.summary_line()}\nStill running after {limit:.0f}s.")
    return "\n\n".join(parts)


@tool(inject=_INJECT)
async def task_output(
    id: Annotated[int, "The task number, e.g. 3 for #3."],
    *,
    task_manager: TaskManager | None = None,
) -> str:
    """Return a task's full result, or its progress so far if still running."""
    if task_manager is None:
        return _NO_MANAGER
    task = task_manager.get(int(id))
    if task is None:
        return f"No background task #{id}."
    if not task.done:
        extra = _progress_line(task)
        return f"{task.summary_line()}" + (f"\n{extra}" if extra else "")
    task.delivered = True
    return f"{task.summary_line()}\n{task.outcome_text(limit=None)}"


@tool(inject=_INJECT)
async def task_cancel(
    id: Annotated[int, "The task number to stop, e.g. 3 for #3."],
    *,
    task_manager: TaskManager | None = None,
) -> str:
    """Stop a running background task."""
    if task_manager is None:
        return _NO_MANAGER
    task = await task_manager.cancel(int(id))
    if task is None:
        return f"No background task #{id}."
    task.delivered = True
    return f"{task.label} is {task.status}."


TASK_TOOLS = [task_status, task_wait, task_output, task_cancel]

__all__ = ["TASK_TOOLS", "task_cancel", "task_output", "task_status", "task_wait"]
