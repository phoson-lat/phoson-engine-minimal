"""Background tasks in the full-screen TUI: the dock, notices and the view.

- **Dock**: a few lines above the composer listing running tasks, tasks
  waiting for approval, and tasks that finished a moment ago.
- **Notices**: a line in the chat when a task finishes or needs approval.
- **View** (Ctrl+B with nothing to send to the background): a picker over
  every task to show its output, cancel it, or answer its approval.

The dock repaints from :class:`~phoson_cli.tasks.TaskManager` listener
events, plus a 1 s tick while something runs so elapsed times advance.
"""

import time
import asyncio
from typing import Any

from prompt_toolkit.formatted_text import FormattedText

from ..tasks_picker import task_row, order_tasks, build_tasks_picker

#: Rows the dock shows before collapsing the rest into "+N more".
DOCK_MAX_ROWS = 4
#: Seconds a finished task stays in the dock.
DOCK_LINGER_SECONDS = 8.0
#: Characters of a task's output shown in the chat from the view.
SHOW_OUTPUT_CHARS = 4000


class TasksDock:
    """Owns the dock rendering, task notices and the tasks view."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._ticker: asyncio.Task | None = None
        self._announced_done: set[int] = set()
        self._announced_approval: set[int] = set()
        manager = app.session.tasks
        self._unsubscribe = manager.add_listener(self._on_task_changed)

    # ── Model ─────────────────────────────────────────────────────────────

    @property
    def manager(self) -> Any:
        return self.app.session.tasks

    def rows(self) -> list[Any]:
        """Tasks the dock shows: approvals, running, recently finished."""
        now = time.monotonic()
        visible = [
            task
            for task in self.manager.tasks()
            if task.approval is not None
            or task.status == "running"
            or (
                task.finished_at is not None
                and now - task.finished_at < DOCK_LINGER_SECONDS
            )
        ]
        return order_tasks(visible)

    def height(self) -> int:
        rows = self.rows()
        if not rows:
            return 0
        return min(len(rows), DOCK_MAX_ROWS) + (1 if len(rows) > DOCK_MAX_ROWS else 0)

    def visible(self) -> bool:
        return bool(self.rows())

    def text(self) -> FormattedText:
        rows = self.rows()
        parts: list[tuple[str, str]] = []
        for task in rows[:DOCK_MAX_ROWS]:
            style = (
                "class:tasks.approval"
                if task.approval is not None
                else "class:tasks.error"
                if task.status == "error"
                else "class:tasks.done"
                if task.done
                else "class:tasks.running"
            )
            line = " " + task_row(task, 40)
            if task.approval is not None:
                line += "  — ctrl+b to answer"
            parts.append((style, line + "\n"))
        if len(rows) > DOCK_MAX_ROWS:
            parts.append(
                ("class:tasks.more", f"  +{len(rows) - DOCK_MAX_ROWS} more · ctrl+b\n")
            )
        return FormattedText(parts)

    # ── Events ────────────────────────────────────────────────────────────

    def _on_task_changed(self, task: Any) -> None:
        sink = self.app.sink
        if task.approval is not None and task.id not in self._announced_approval:
            self._announced_approval.add(task.id)
            sink.notify(
                "warn",
                f"{task.label} needs your approval for {task.approval.tool}: "
                f"{task.approval.summary[:80]} — press Ctrl+B to answer.",
            )
        elif task.approval is None:
            self._announced_approval.discard(task.id)
        if task.done and task.id not in self._announced_done:
            self._announced_done.add(task.id)
            level = "error" if task.status == "error" else "info"
            verb = {"done": "finished", "error": "failed", "cancelled": "cancelled"}
            sink.notify(
                level,
                f"{task.label} {verb.get(task.status, task.status)} after "
                f"{task.elapsed:.0f}s — {task.title[:60]}",
            )
        if task.status == "running":
            self._ensure_ticker()
        self._invalidate()

    def _ensure_ticker(self) -> None:
        if self._ticker is not None and not self._ticker.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ticker = loop.create_task(self._tick())

    async def _tick(self) -> None:
        # Advance elapsed times, and let finished rows linger then disappear.
        while self.manager.running() or self.rows():
            await asyncio.sleep(1.0)
            self._invalidate()

    def _invalidate(self) -> None:
        application = getattr(self.app, "app", None)
        if application is not None:
            application.invalidate()

    def close(self) -> None:
        self._unsubscribe()
        if self._ticker is not None:
            self._ticker.cancel()

    # ── View ──────────────────────────────────────────────────────────────

    async def open_view(self) -> None:
        """Ctrl+B with nothing to detach: browse and act on the tasks."""
        app = self.app
        tasks = self.manager.tasks()
        if not tasks:
            app.sink.notify("info", "No background tasks.")
            return
        if app._active_float is not None:
            return
        picker = build_tasks_picker(
            tasks, theme=app.theme, invalidate=app.app.invalidate
        )
        result = await app.run_float_picker(picker)
        if result.cancelled or result.task_id is None:
            return
        await self.apply(result.action, result.task_id)

    async def apply(self, action: str | None, task_id: int) -> None:
        manager = self.manager
        sink = self.app.sink
        task = manager.get(task_id)
        if task is None:
            return
        if action == "cancel":
            await manager.cancel(task_id)
            sink.notify("info", f"{task.label} is {task.status}.")
        elif action in {"approve", "deny"}:
            allow = action == "approve"
            if manager.resolve_approval(task_id, allow):
                sink.notify(
                    "info", f"{task.label}: {'approved' if allow else 'denied'}."
                )
            else:
                sink.notify("info", f"{task.label} is not waiting for an approval.")
        elif action == "show":
            body = (
                task.outcome_text(limit=SHOW_OUTPUT_CHARS)
                if task.done
                else (task.detail or "still running")
            )
            sink.notify("info", f"{task.summary_line()}\n{body}")


__all__ = ["DOCK_MAX_ROWS", "TasksDock"]
