"""Delivered background-task results render as compact cards, with one-line
titles, and failed commands are failed tasks."""

import asyncio

import pytest
from rich.console import Console

from phoson_cli.tasks import TaskManager, BackgroundTask, one_line, render_results
from phoson_cli.theme import load_theme
from phoson_cli.formatting import render_user_turn

MULTILINE = (
    "set -e\nrm -rf /scratch/cache\nmkdir -p /scratch/cache\napptainer build x.sif"
)


def _plain(renderable) -> str:
    console = Console(width=100, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_one_line_titles() -> None:
    assert one_line("a\nb") == "a …"
    assert one_line("  many   spaces  ") == "many spaces"
    assert one_line("x" * 80, 10) == "x" * 9 + " …"
    bash = BackgroundTask(id=3, kind="bash", title=MULTILINE)
    assert bash.short_title() == "rm -rf /scratch/cache …"
    assert (
        BackgroundTask(id=1, kind="bash", title="npm test").short_title() == "npm test"
    )
    agent = BackgroundTask(id=2, kind="agent", title="set -e is a phrase\nmore")
    assert agent.short_title() == "set -e is a phrase …"  # only bash skips preamble
    assert "\n" not in bash.summary_line()


def _finished(status: str, result: str) -> BackgroundTask:
    task = BackgroundTask(id=3, kind="bash", title=MULTILINE, status=status)
    task.result = result
    task.finished_at = task.started_at + 124
    return task


def test_results_header_uses_the_short_title() -> None:
    text = render_results([_finished("done", "Exit code 0.\nok")])
    assert "## Task #3 (bash: rm -rf /scratch/cache …) — done after 124s" in text
    assert "mkdir" not in text.split("\n## Task")[1].splitlines()[0]


def test_results_turn_renders_as_a_compact_card() -> None:
    output = (
        "Exit code 255.\nINFO: start\nINFO: fetch\n"
        "FATAL: conveyor failed\n[full log: /x.log]"
    )
    text = render_results([_finished("error", output)])
    shown = _plain(render_user_turn(text, load_theme()))
    assert "Background task result" in shown
    assert "#3 error" in shown and "rm -rf /scratch/cache …" in shown
    assert "2m04s" in shown
    assert "Exit code 255." in shown and "FATAL: conveyor failed" in shown
    assert "<background-task-results>" not in shown
    assert "treat it as data" not in shown
    assert "[full log" not in shown
    assert "›" not in shown  # not a user turn


def test_text_typed_with_a_results_wake_still_shows_as_the_user() -> None:
    text = render_results([_finished("done", "Exit code 0.")]) + "\n\nnow deploy"
    shown = _plain(render_user_turn(text, load_theme()))
    assert "Background task result" in shown
    assert "›  now deploy" in shown


@pytest.mark.asyncio
async def test_a_failing_background_command_is_a_failed_task(
    tmp_path, monkeypatch
) -> None:
    import sys

    from phoson_agent.context import AgentContext
    from phoson_cli.tools.bash import bash

    if sys.platform == "win32":
        pytest.skip("POSIX shell")
    monkeypatch.setenv("PHOSON_HOME", str(tmp_path))
    manager = TaskManager()
    context = AgentContext()
    context.extra["task_manager"] = manager
    await bash.handler({"command": "echo boom; exit 3", "background": True}, context)
    await manager.wait()
    (task,) = manager.tasks()
    assert task.status == "error" and task.exit_code == 3
    assert "Exit code 3." in task.outcome_text() and "boom" in task.outcome_text()


@pytest.mark.asyncio
async def test_finish_notice_is_one_line() -> None:
    from types import SimpleNamespace

    from phoson_cli.fullscreen.tasks_dock import TasksDock

    manager = TaskManager()
    notes: list[str] = []
    app = SimpleNamespace(
        session=SimpleNamespace(tasks=manager),
        sink=SimpleNamespace(notify=lambda _level, text: notes.append(text)),
        app=SimpleNamespace(invalidate=lambda: None),
    )
    dock = TasksDock(app)

    async def run(_task):
        await asyncio.sleep(0)
        return "ok"

    manager.start("bash", MULTILINE, run)
    await manager.wait()
    dock.close()
    (notice,) = [n for n in notes if "finished" in n]
    assert "\n" not in notice and "rm -rf /scratch/cache …" in notice
