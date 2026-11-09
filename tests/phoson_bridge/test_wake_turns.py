"""Tests for autonomous wake-ups (monitors / background jobs).

Run them with the engine's interpreter (the bridge needs `phoson_cli`):

    ../phoson-engine-minimal/.venv/bin/python bridge/tests/test_wake_turns.py

No pytest: asserts and a `main()` that prints what it checks. What the UI sees:

1. When building a session, the bridge registers its `operation_starter` and
   starts the controller's wake loop (previously nobody started it and a monitor
   was only delivered with the user's next message).
2. An autonomous turn that runs ends with `session.assistant.done` (with the
   outcome status) so the GUI closes the message.
3. If a user turn consumed the wakes first (nothing ran), the bridge emits
   nothing.
4. With an older engine (the wake returns no outcome) the close is inferred from
   `session.user_message`.
5. Cancelling the autonomous turn emits `cancelled` and propagates the
   cancellation.
6. With the plugin loaded from `[defaults].plugins` (`enable_*` flag off) an
   engine <= 0.57 does not start the loop: the bridge starts it itself.
"""

import sys
import asyncio
from types import SimpleNamespace
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phoson_bridge.server as server  # noqa: E402
from phoson_bridge.sink import GuiSink  # noqa: E402


class _FakeController:
    def __init__(self, sink: GuiSink) -> None:
        self.sink = sink
        self.starter: Any = None
        self.loop_started = 0

    def set_operation_starter(self, starter: Any) -> None:
        self.starter = starter

    def start_monitor_wake_loop(self) -> None:
        self.loop_started += 1


def _manager() -> tuple[server.SessionManager, list[tuple[str, dict]], str]:
    emitted: list[tuple[str, dict]] = []

    def emit(method: str, params: dict, *, life: bool = True) -> None:
        emitted.append((method, params))

    manager = server.SessionManager(SimpleNamespace(), emit)
    key = "sess-1"
    sink = GuiSink(key, lambda method, params: emitted.append((method, params)))
    manager._repls[key] = SimpleNamespace(_controller=_FakeController(sink))  # type: ignore[assignment]
    return manager, emitted, key


def _done(emitted: list[tuple[str, dict]]) -> list[dict]:
    return [p for m, p in emitted if m == "session.assistant.done"]


async def test_wires_loop_and_starter() -> None:
    manager, _, key = _manager()
    repl = manager._repls[key]
    manager._wire_wakes(key, repl)  # type: ignore[arg-type]
    controller = repl._controller
    assert controller.loop_started == 1
    assert callable(controller.starter)

    async def wake() -> None:
        return None

    task = controller.starter(wake(), "wake")
    assert isinstance(task, asyncio.Task)
    await task


async def test_wake_turn_emits_done_with_outcome() -> None:
    manager, emitted, key = _manager()
    finished: list[str] = []
    manager.on_wake_turn_done = finished.append
    sink = manager._repls[key]._controller.sink

    async def wake() -> Any:
        sink.on_user_message("[MONITOR EVENTS] ...", None)
        return SimpleNamespace(status="error", error_code="auth", final_content=None)

    await manager._run_wake_turn(key, wake())
    (done,) = _done(emitted)
    assert done["status"] == "error" and done["errorCode"] == "auth"
    assert done["source"] == "wake"
    assert finished == [key]


async def test_consumed_wake_emits_nothing() -> None:
    manager, emitted, key = _manager()

    async def wake() -> None:
        return None  # a user turn already consumed them

    await manager._run_wake_turn(key, wake())
    assert _done(emitted) == []


async def test_old_engine_closes_from_user_message() -> None:
    manager, emitted, key = _manager()
    sink = manager._repls[key]._controller.sink

    async def wake() -> None:
        sink.on_user_message("[MONITOR EVENTS] ...", None)
        return None  # older engine: no outcome

    await manager._run_wake_turn(key, wake())
    (done,) = _done(emitted)
    assert done["status"] == "done"


async def test_cancel_emits_cancelled() -> None:
    manager, emitted, key = _manager()
    sink = manager._repls[key]._controller.sink
    started = asyncio.Event()

    async def wake() -> None:
        sink.on_user_message("[MONITOR EVENTS] ...", None)
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(manager._run_wake_turn(key, wake()))
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("la cancelación debe propagarse")
    (done,) = _done(emitted)
    assert done["status"] == "cancelled"


class _GatedController(_FakeController):
    """Controller of an engine <= 0.57: the loop depends on the flags."""

    def __init__(self, sink: GuiSink) -> None:
        super().__init__(sink)
        self._monitor_wake_task: Any = None
        self.ticks = 0

    async def _monitor_wake_loop(self) -> None:
        self.ticks += 1
        await asyncio.sleep(60)


class _WakePlugin:
    def drain_pending_wakes(self, session_id: Any) -> list:
        return []


async def test_starts_loop_for_listed_plugin() -> None:
    manager, _, key = _manager()
    sink = manager._repls[key]._controller.sink
    controller = _GatedController(sink)
    repl = SimpleNamespace(
        _controller=controller,
        engine=SimpleNamespace(_loaded_plugins=[_WakePlugin()]),
    )
    manager._wire_wakes(key, repl)  # type: ignore[arg-type]
    task = controller._monitor_wake_task
    assert isinstance(task, asyncio.Task)
    await asyncio.sleep(0)
    assert controller.ticks == 1
    # With no wake providers loaded, nothing is started.
    idle = _GatedController(sink)
    manager._wire_wakes(
        key,
        SimpleNamespace(_controller=idle, engine=SimpleNamespace(_loaded_plugins=[])),
    )  # type: ignore[arg-type]
    assert idle._monitor_wake_task is None
    task.cancel()


async def _run_all() -> None:
    for test in (
        test_wires_loop_and_starter,
        test_wake_turn_emits_done_with_outcome,
        test_consumed_wake_emits_nothing,
        test_old_engine_closes_from_user_message,
        test_cancel_emits_cancelled,
        test_starts_loop_for_listed_plugin,
    ):
        await test()
        print(f"ok  {test.__name__}")


def main() -> None:
    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
