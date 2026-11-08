"""Background tasks UI: approval queue, /tasks, the tasks view and the dock."""

import asyncio
from types import SimpleNamespace

import pytest

from phoson_cli.tasks import TaskManager, current_background_task
from phoson_cli.tasks_picker import task_row, order_tasks, format_elapsed


async def _returns(value: str, delay: float = 0.0):
    async def run(_task):
        await asyncio.sleep(delay)
        return value

    return run


# ─── Approval queue ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_task_waits_for_the_users_answer() -> None:
    manager = TaskManager()
    answers: list[bool] = []

    async def run(task):
        assert current_background_task() is task
        answers.append(await manager.request_approval(task, "bash", "rm -rf build"))
        return "done"

    task = manager.start("agent", "cleanup", run)
    await asyncio.sleep(0.01)
    assert manager.pending_approvals() == [task]
    assert task.detail.startswith("waiting for approval")
    assert manager.resolve_approval(task.id, False) is True
    await manager.wait()
    assert answers == [False] and task.approval is None
    assert manager.resolve_approval(task.id, True) is False  # nothing pending
    assert current_background_task() is None  # not set outside the task


@pytest.mark.asyncio
async def test_controller_routes_background_asks_to_the_queue(tmp_path) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    with patch("phoson_cli.controller.build_chat"):
        controller = SessionController(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), MagicMock()
        )
    controller.confirmation = MagicMock()
    controller.confirmation.confirm_bash = AsyncMock(return_value=True)
    try:
        results: list[bool] = []

        async def run(_task):
            results.append(
                await controller._ask_permission("bash", {"command": "make deploy"})
            )
            return "ok"

        task = controller.tasks.start("agent", "deploy", run)
        await asyncio.sleep(0.01)
        assert task.approval is not None
        assert task.approval.summary == "make deploy"
        controller.confirmation.confirm_bash.assert_not_called()  # no dialog
        controller.tasks.resolve_approval(task.id, True)
        await controller.tasks.wait()
        assert results == [True]
    finally:
        await controller.shutdown()


# ─── /tasks ──────────────────────────────────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _handler(manager: TaskManager):
    from phoson_cli.commands import CommandHandler

    handler = CommandHandler.__new__(CommandHandler)
    handler.session = SimpleNamespace(tasks=manager)
    handler.host = _Host()
    return handler


async def _tasks(handler, args: str = "") -> None:
    from phoson_cli.commands import Command

    await handler._cmd_tasks(Command(name="/tasks", args=args))


@pytest.mark.asyncio
async def test_tasks_command() -> None:
    manager = TaskManager()
    handler = _handler(manager)
    await _tasks(handler)
    assert handler.host.info[-1] == "No background tasks."

    done = manager.start("agent", "summarize docs", await _returns("THE SUMMARY"))
    slow = manager.start("bash", "npm run dev", await _returns("x", 10))
    await manager.wait([done.id])

    await _tasks(handler)
    listing = handler.host.info[-1]
    assert "#1" in listing and "#2" in listing and "running" in listing

    await _tasks(handler, "#1")
    assert "THE SUMMARY" in handler.host.info[-1]

    await _tasks(handler, f"cancel {slow.id}")
    assert "cancelled" in handler.host.info[-1]

    await _tasks(handler, "approve 1")
    assert "not waiting" in handler.host.errors[-1]
    await _tasks(handler, "bogus")
    assert handler.host.errors[-1].startswith("Usage: /tasks")
    await _tasks(handler, "9")
    assert "No background task #9" in handler.host.errors[-1]


@pytest.mark.asyncio
async def test_tasks_command_answers_approvals() -> None:
    manager = TaskManager()
    handler = _handler(manager)

    async def run(task):
        return "ok" if await manager.request_approval(task, "bash", "ls") else "no"

    task = manager.start("agent", "x", run)
    await asyncio.sleep(0.01)
    await _tasks(handler)
    assert "needs approval: bash ls" in handler.host.info[-1]
    await _tasks(handler, f"deny {task.id}")
    await manager.wait()
    assert task.result == "no"


def test_tasks_is_run_safe_and_listed() -> None:
    from phoson_cli.command_catalog import COMMAND_SPECS, is_run_safe_command

    assert is_run_safe_command("/tasks")
    assert any("/tasks" in spec.names for spec in COMMAND_SPECS)


# ─── View helpers ────────────────────────────────────────────────────────────


def _task(id_: int, status: str, approval=None, detail: str = ""):
    return SimpleNamespace(
        id=id_,
        label=f"#{id_}",
        title=f"task {id_}",
        status=status,
        approval=approval,
        detail=detail,
        elapsed=75.0,
    )


def test_order_puts_approvals_then_running_first() -> None:
    approval = SimpleNamespace(tool="bash", summary="rm x")
    tasks = [
        _task(1, "done"),
        _task(2, "running"),
        _task(3, "running", approval),
        _task(4, "running"),
    ]
    assert [t.id for t in order_tasks(tasks)] == [3, 4, 2, 1]


def test_task_row_and_elapsed() -> None:
    assert format_elapsed(5) == "5s" and format_elapsed(75) == "1:15"
    assert format_elapsed(3700) == "1h01"
    row = task_row(_task(2, "running", detail="PASS auth.spec"), 20)
    assert row.startswith("⚙ #2") and "1:15" in row and "PASS auth.spec" in row
    approval = SimpleNamespace(tool="bash", summary="rm -rf build")
    assert "needs approval: bash rm -rf build" in task_row(
        _task(3, "running", approval)
    )


# ─── Dock ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dock_rows_notices_and_height() -> None:
    from phoson_cli.fullscreen.tasks_dock import DOCK_MAX_ROWS, TasksDock

    manager = TaskManager()
    notes: list[tuple[str, str]] = []
    app = SimpleNamespace(
        session=SimpleNamespace(tasks=manager),
        sink=SimpleNamespace(notify=lambda level, text: notes.append((level, text))),
        app=SimpleNamespace(invalidate=lambda: None),
    )
    dock = TasksDock(app)
    assert not dock.visible() and dock.height() == 0

    task = manager.start("bash", "npm test", await _returns("ok"))
    assert dock.visible() and "#1" in str(dock.text())
    await manager.wait()
    assert any("#1 finished" in text for _, text in notes)
    assert dock.visible()  # lingers for a moment after finishing

    for i in range(DOCK_MAX_ROWS + 1):
        manager.start("agent", f"a{i}", await _returns("x", 10))
    assert dock.height() == DOCK_MAX_ROWS + 1
    assert "more" in str(dock.text())
    await manager.shutdown()
    dock.close()
    assert task.delivered is False


# ─── TUI wiring ──────────────────────────────────────────────────────────────


def test_footer_hints_ctrl_b_while_a_detachable_call_runs(monkeypatch) -> None:
    from phoson_cli.fullscreen.header_model import HeaderModel

    controller = SimpleNamespace(detachable_running=lambda: [object()])
    app = SimpleNamespace(
        _chat_pane=SimpleNamespace(lines_below=lambda: 0),
        _auto_scroll=True,
        _active_float=None,
        _shell_mode=False,
        _is_run_in_flight=lambda: True,
        _pending_turns=[],
        session=SimpleNamespace(
            controller=controller,
            current_model="m",
            config=SimpleNamespace(provider="p"),
        ),
    )
    monkeypatch.setattr(
        "phoson_cli.fullscreen.header_model.exit_armed", lambda _a: False
    )
    model = HeaderModel.__new__(HeaderModel)
    model.app = app
    monkeypatch.setattr(model, "_footer_width", lambda: 200)
    assert "ctrl+b background" in str(model.get_footer_text().value)


def test_quitting_with_running_tasks_needs_a_second_press() -> None:
    from phoson_cli.fullscreen.lifecycle_controller import request_exit

    exits: list[bool] = []
    notes: list[str] = []
    app = SimpleNamespace(
        _is_run_in_flight=lambda: False,
        session=SimpleNamespace(tasks=SimpleNamespace(running=lambda: [object()])),
        sink=SimpleNamespace(notify=lambda _level, text: notes.append(text)),
        app=SimpleNamespace(exit=lambda: exits.append(True)),
    )
    request_exit(app)
    assert exits == [] and "Press again to quit" in notes[-1]
    request_exit(app)
    assert exits == [True]
