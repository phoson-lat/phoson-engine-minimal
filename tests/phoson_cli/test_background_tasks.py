"""Background tasks: manager lifecycle, delivery to the main agent (mid-turn
and idle), the task_* tools, and background sub-agents."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from phoson_cli.tasks import (
    TaskManager,
    TaskDeliveryMiddleware,
    render_results,
    mark_subagent_context,
    reset_subagent_context,
)
from phoson_llm.schemas import Message, TextBlock, ModelConfig, ToolResultBlock

pytestmark = pytest.mark.asyncio


async def _returns(value: str, delay: float = 0.0):
    async def run(_task):
        await asyncio.sleep(delay)
        return value

    return run


# ─── Manager ─────────────────────────────────────────────────────────────────


async def test_task_runs_in_background_and_records_its_result() -> None:
    manager = TaskManager()
    seen: list[str] = []
    manager.add_listener(lambda task: seen.append(task.status))
    task = manager.start("agent", "look into X", await _returns("found it", 0.01))
    assert task.label == "#1" and task.status == "running"
    await manager.wait([task.id])
    assert task.status == "done" and task.result == "found it"
    assert seen[0] == "running" and seen[-1] == "done"
    assert manager.get(1) is task and manager.running() == []


async def test_failures_and_cancellation_are_outcomes() -> None:
    manager = TaskManager()

    async def boom(_task):
        raise ValueError("bad input")

    failed = manager.start("agent", "fails", boom)
    slow = manager.start("agent", "slow", await _returns("never", 10))
    await manager.wait([failed.id], timeout=1)
    assert failed.status == "error" and "bad input" in (failed.error or "")
    await manager.cancel(slow.id)
    assert slow.status == "cancelled"
    assert "Cancelled" in slow.outcome_text()


async def test_running_limit() -> None:
    manager = TaskManager(max_running=1)
    manager.start("agent", "a", await _returns("x", 10))
    with pytest.raises(RuntimeError, match="limit 1"):
        manager.start("agent", "b", await _returns("y"))
    await manager.shutdown()
    assert manager.running() == []


async def test_wait_times_out_without_cancelling() -> None:
    manager = TaskManager()
    task = manager.start("agent", "slow", await _returns("late", 10))
    await manager.wait([task.id], timeout=0.01)
    assert task.status == "running"
    await manager.shutdown()


async def test_results_are_delivered_once_and_framed_as_task_output() -> None:
    manager = TaskManager()
    task = manager.start("agent", "summarize", await _returns("the summary"))
    await manager.wait()
    assert manager.pending_wakes() == [task]
    drained = manager.drain_pending_wakes()
    assert drained == [task] and manager.drain_pending_wakes() == []
    text = manager.render_wake_message(drained)
    assert "<background-task-results>" in text
    assert "not a message from the user" in text
    assert "#1" in text and "the summary" in text


async def test_long_results_are_clipped_inline() -> None:
    manager = TaskManager()
    task = manager.start("agent", "big", await _returns("x" * 10_000))
    await manager.wait()
    assert "task_output(1)" in render_results([task])
    assert len(task.outcome_text(limit=None)) == 10_000


async def test_monitor_status_counts_running_tasks() -> None:
    manager = TaskManager()
    assert manager.monitor_status() is None
    manager.start("agent", "a", await _returns("x", 10))
    assert manager.monitor_status() == "1 task"
    await manager.shutdown()


# ─── Mid-turn delivery ───────────────────────────────────────────────────────


async def _finished_manager() -> TaskManager:
    manager = TaskManager()
    manager.start("agent", "research", await _returns("answer"))
    await manager.wait()
    return manager


async def test_delivery_merges_into_trailing_tool_results() -> None:
    manager = await _finished_manager()
    middleware = TaskDeliveryMiddleware(manager)
    history = [
        Message(role="user", content="go"),
        Message(role="assistant", content="working"),
        Message(role="user", content=[ToolResultBlock("t1", "ok", error=False)]),
    ]
    out = await middleware.on_before_llm(history, ModelConfig(model="m"))
    assert len(out) == 3
    blocks = out[-1].content
    assert isinstance(blocks[0], ToolResultBlock)
    assert isinstance(blocks[-1], TextBlock) and "answer" in blocks[-1].text
    # Delivered once.
    again = await middleware.on_before_llm(out, ModelConfig(model="m"))
    assert again == out


async def test_delivery_appends_after_an_assistant_message() -> None:
    manager = await _finished_manager()
    out = await TaskDeliveryMiddleware(manager).on_before_llm(
        [Message(role="assistant", content="hi")], ModelConfig(model="m")
    )
    assert [m.role for m in out] == ["assistant", "user"]


async def test_delivery_skips_sub_agent_calls() -> None:
    manager = await _finished_manager()
    middleware = TaskDeliveryMiddleware(manager)
    token = mark_subagent_context()
    try:
        history = [Message(role="user", content="sub task")]
        assert (
            await middleware.on_before_llm(history, ModelConfig(model="m")) == history
        )
    finally:
        reset_subagent_context(token)
    assert manager.undelivered()  # still waiting for the main agent


# ─── task_* tools ────────────────────────────────────────────────────────────


async def _call(tool, **args):
    return await tool.handler(args, SimpleNamespace(extra=args.pop("_extra", {})))


async def test_task_tools() -> None:
    from phoson_cli.tools.tasks import task_wait, task_cancel, task_output, task_status

    manager = TaskManager()
    extra = {"task_manager": manager}
    fast = manager.start("agent", "fast one", await _returns("fast result"))
    slow = manager.start("agent", "slow one", await _returns("slow", 10))

    waited = await task_wait.handler({"ids": [fast.id], "timeout": 5}, _ctx(extra))
    assert "fast result" in waited and fast.delivered

    status = await task_status.handler({}, _ctx(extra))
    assert "#1" in status and "#2" in status and "running" in status

    output = await task_output.handler({"id": slow.id}, _ctx(extra))
    assert "running" in output

    cancelled = await task_cancel.handler({"id": slow.id}, _ctx(extra))
    assert "cancelled" in cancelled
    assert "No background task #9" in await task_output.handler({"id": 9}, _ctx(extra))
    assert "not available" in await task_status.handler({}, _ctx({}))


def _ctx(extra: dict):
    from phoson_agent.context import AgentContext

    context = AgentContext()
    context.extra.update(extra)
    return context


# ─── Background sub-agents ───────────────────────────────────────────────────


def _agent_extra(manager: TaskManager | None) -> dict:
    from phoson_cli.tools import read_file

    extra = {
        "chat": object(),
        "available_tools": {"read_file": read_file},
        "default_model": "m",
        "max_iterations": 5,
    }
    if manager is not None:
        extra["task_manager"] = manager
    return extra


async def test_agent_background_returns_at_once_and_delivers_later() -> None:
    from phoson_cli.tools import subagent

    release = asyncio.Event()

    async def fake_run(**kwargs):
        await release.wait()
        return f"done: {kwargs['task']}", None

    manager = TaskManager()
    with patch.object(subagent, "_run_one_subagent", fake_run):
        reply = await subagent.agent.handler(
            {"task": "audit auth", "background": True}, _ctx(_agent_extra(manager))
        )
        assert "#1" in reply and "Started in the background" in reply
        (task,) = manager.tasks()
        assert task.kind == "agent" and task.status == "running"
        release.set()
        await manager.wait()
    assert task.result == "done: audit auth"
    assert manager.undelivered() == [task]


async def test_agents_background_starts_one_task_each() -> None:
    from phoson_cli.tools import subagent

    async def fake_run(**kwargs):
        return kwargs["task"].upper(), None

    manager = TaskManager()
    with patch.object(subagent, "_run_one_subagent", fake_run):
        reply = await subagent.agents.handler(
            {"tasks": ["a", "b"], "background": True}, _ctx(_agent_extra(manager))
        )
        await manager.wait()
    assert "#1" in reply and "#2" in reply
    assert sorted(t.result for t in manager.tasks()) == ["A", "B"]


async def test_background_without_a_manager_runs_in_the_foreground() -> None:
    from phoson_cli.tools import subagent

    async def fake_run(**kwargs):
        return "inline result", None

    with patch.object(subagent, "_run_one_subagent", fake_run):
        reply = await subagent.agent.handler(
            {"task": "x", "background": True}, _ctx(_agent_extra(None))
        )
    assert reply == "inline result"


# ─── Controller wiring ───────────────────────────────────────────────────────


async def test_controller_wires_tasks(tmp_path) -> None:
    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    class _Sink:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
    with patch("phoson_cli.controller.build_chat"):
        controller = SessionController(config, _Sink())  # type: ignore[arg-type]
    try:
        names = [tool.name for tool in controller.engine.tools]
        assert {"task_status", "task_wait", "task_output", "task_cancel"} <= set(names)
        assert controller.engine.context.extra["task_manager"] is controller.tasks
        middlewares = controller.engine.middlewares
        assert middlewares[-1] is controller.permission_middleware
        assert type(middlewares[-2]).__name__ == "TaskDeliveryMiddleware"
        assert controller.tasks in controller._wake_providers()

        assert controller._monitor_wake_task is None
        controller.tasks.start("agent", "x", await _returns("y"))
        assert controller._monitor_wake_task is not None  # started by the task
    finally:
        await controller.shutdown()


# ─── Detaching running calls (Ctrl+B) ────────────────────────────────────────


async def test_tool_detacher_hands_calls_to_the_manager() -> None:
    from phoson_cli.tasks import ToolDetacher

    manager = TaskManager()
    detacher = ToolDetacher(manager)
    release = asyncio.Event()

    async def body():
        await release.wait()
        return ("12 passed", None, False, None)

    call = SimpleNamespace(tool_name="bash", args={"command": "pytest -q"})
    running = asyncio.create_task(body())
    future = detacher.track(call, running)
    assert detacher.live_calls() == [call]

    (task,) = detacher.detach_all()
    assert task.kind == "bash" and task.title == "bash: pytest -q"
    assert "#1" in future.result() and "background" in future.result()
    assert detacher.live_calls() == [] and detacher.detach_all() == []

    release.set()
    await manager.wait()
    assert task.status == "done" and task.result == "12 passed"


async def test_detached_tool_error_is_a_failed_task() -> None:
    from phoson_cli.tasks import ToolDetacher

    manager = TaskManager()
    detacher = ToolDetacher(manager)

    async def body():
        return ("boom", "boom", True, None)

    call = SimpleNamespace(tool_name="web_fetch", args={"url": "https://x"})
    detacher.track(call, asyncio.create_task(body()))
    (task,) = detacher.detach_all()
    await manager.wait()
    assert task.kind == "tool" and task.status == "error" and "boom" in task.error


async def test_bash_background_streams_to_a_private_log(tmp_path, monkeypatch) -> None:
    import os
    import sys
    import stat

    from phoson_cli.tools.bash import bash

    if sys.platform == "win32":
        pytest.skip("POSIX shell")
    monkeypatch.setenv("PHOSON_HOME", str(tmp_path))
    manager = TaskManager()
    reply = await bash.handler(
        {"command": "echo first; echo second", "background": True},
        _ctx({"task_manager": manager}),
    )
    assert "#1" in reply
    (task,) = manager.tasks()
    await manager.wait()
    assert task.status == "done"
    assert "Exit code 0" in task.result and "second" in task.result
    (log,) = (tmp_path / "tasks").iterdir()
    assert log.read_text() == "first\nsecond\n"
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600


async def test_detached_bash_outlives_its_foreground_timeout() -> None:
    import sys

    from phoson_agent.detach import set_detach_event
    from phoson_cli.tools.bash import _run_bash

    if sys.platform == "win32":
        pytest.skip("POSIX shell")
    event = asyncio.Event()

    async def run():
        set_detach_event(event)
        return await _run_bash("sleep 0.4; echo survived", timeout=0.1)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.02)
    event.set()  # sent to the background before the 0.1 s deadline
    assert "survived" in await task

    plain = await _run_bash("sleep 0.4; echo late", timeout=0.1)
    assert "timed out" in plain
