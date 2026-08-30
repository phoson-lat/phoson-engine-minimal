"""Unit tests for monitor kinds with an injected fake clock.

Deterministic: no real timers. The fake ``sleep_fn`` blocks the kind loop
until the test calls ``clock.advance()``, so every tick is explicit.
Command tests do spawn real (tiny) subprocesses — that is the only real
I/O involved.
"""

import asyncio
from pathlib import Path

import pytest

from phoson_plugin_monitor.kinds import (
    FileKind,
    CommandKind,
    IntervalKind,
    run_kind,
    known_kinds,
    validate_spec,
)
from phoson_plugin_monitor.storage import MonitorDef


class FakeClock:
    """sleep_fn that blocks the caller until the test advances it."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._release: asyncio.Event | None = None

    def sleep(self, seconds: float):
        clock = self

        async def _sleep() -> None:
            clock.sleeps.append(seconds)
            if clock._release is not None:
                clock._release.clear()
            clock._release = asyncio.Event()
            await clock._release.wait()

        return _sleep()

    def advance(self) -> None:
        """Release the currently blocked sleep (one tick)."""
        assert self._release is not None, "no pending sleep to advance"
        self._release.set()

    @property
    def has_pending_sleep(self) -> bool:
        return self._release is not None and not self._release.is_set()


def _defn(kind: str, spec: dict) -> MonitorDef:
    return MonitorDef(name="m", kind=kind, spec=spec)


async def _poll_until(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not met before timeout")
        await asyncio.sleep(0.01)


# ── validate_spec ──────────────────────────────────────────────────────────────


class TestValidateSpec:
    def test_known_kinds(self) -> None:
        assert known_kinds() == ["interval", "file", "command"]

    def test_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="Unknown monitor kind"):
            validate_spec("nope", {})

    def test_spec_must_be_dict(self) -> None:
        with pytest.raises(ValueError, match="spec must be a JSON object"):
            validate_spec("interval", "5")  # type: ignore[arg-type]

    def test_interval(self) -> None:
        assert validate_spec("interval", {"seconds": 5}) == {
            "seconds": 5.0,
            "once": False,
        }
        assert validate_spec("interval", {"seconds": 5, "once": True})["once"]

    @pytest.mark.parametrize(
        "bad",
        [{}, {"seconds": 0}, {"seconds": "x"}, {"seconds": 5, "once": "yes"}],
    )
    def test_interval_invalid(self, bad: dict) -> None:
        with pytest.raises(ValueError):
            validate_spec("interval", bad)

    def test_file(self) -> None:
        assert validate_spec("file", {"path": "a.log"}) == {
            "path": "a.log",
            "event": "changed",
            "poll_seconds": 2.0,
        }
        assert validate_spec("file", {"path": "x", "event": "both"})["event"]

    @pytest.mark.parametrize(
        "bad",
        [{}, {"path": "a", "event": "nope"}, {"path": "a", "poll_seconds": 0.1}],
    )
    def test_file_invalid(self, bad: dict) -> None:
        with pytest.raises(ValueError):
            validate_spec("file", bad)

    def test_command(self) -> None:
        assert validate_spec("command", {"command": "ls"}) == {
            "command": "ls",
            "interval_seconds": 60.0,
            "timeout_seconds": 60.0,
        }

    def test_command_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            validate_spec("command", {"command": "ls", "timeout_seconds": 0})


# ── interval ───────────────────────────────────────────────────────────────────


class TestIntervalKind:
    async def test_fires_after_seconds_and_loops(self) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            IntervalKind.run(
                _defn("interval", {"seconds": 5}), fire, sleep_fn=clock.sleep
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        assert clock.has_pending_sleep
        assert fired == []

        clock.advance()  # first tick → fire
        await asyncio.sleep(0)
        assert fired == [{"interval_seconds": 5.0}]
        # once=false keeps looping: the task must be blocked on the next sleep.
        await _poll_until(lambda: clock.has_pending_sleep)

        clock.advance()  # second tick → another fire
        await asyncio.sleep(0)
        assert len(fired) == 2
        assert clock.sleeps == [5.0, 5.0, 5.0]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_once_stops_after_first_fire(self) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            IntervalKind.run(
                _defn("interval", {"seconds": 2, "once": True}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        clock.advance()
        await asyncio.sleep(0)
        assert len(fired) == 1
        assert task.done()  # natural completion
        assert clock.sleeps == [2.0]


# ── file ───────────────────────────────────────────────────────────────────────


class TestFileKind:
    async def test_fires_on_created(self, tmp_path: Path) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        target = tmp_path / "out.bin"
        task = asyncio.create_task(
            FileKind.run(
                _defn("file", {"path": str(target), "event": "created"}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        clock.advance()  # tick 1: no file yet
        await asyncio.sleep(0)
        assert fired == []

        target.write_bytes(b"hello")
        clock.advance()  # tick 2: file appeared
        await asyncio.sleep(0)
        assert len(fired) == 1
        assert str(target) in fired[0]["created"]
        assert fired[0]["changed"] == []
        task.cancel()

    async def test_fires_on_change(self, tmp_path: Path) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        target = tmp_path / "log.txt"
        target.write_text("v1")
        task = asyncio.create_task(
            FileKind.run(
                _defn("file", {"path": str(target), "event": "changed"}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        clock.advance()  # tick 1: baseline
        await asyncio.sleep(0)
        assert fired == []

        target.write_text("v2-longer")  # size differs → change
        clock.advance()  # tick 2
        await asyncio.sleep(0)
        assert len(fired) == 1
        assert str(target) in fired[0]["changed"]
        task.cancel()

    async def test_glob_matches(self, tmp_path: Path) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        (tmp_path / "a.log").write_text("x")
        task = asyncio.create_task(
            FileKind.run(
                _defn("file", {"path": str(tmp_path / "*.log"), "event": "created"}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        clock.advance()  # baseline sees a.log
        await asyncio.sleep(0)
        assert fired == []

        (tmp_path / "b.log").write_text("y")
        clock.advance()
        await asyncio.sleep(0)
        assert len(fired) == 1
        assert any(p.endswith("b.log") for p in fired[0]["created"])
        task.cancel()

    async def test_no_fire_when_unchanged(self, tmp_path: Path) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            FileKind.run(
                _defn("file", {"path": str(tmp_path / "nope.log")}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        for _ in range(3):
            clock.advance()
            await asyncio.sleep(0)
        assert fired == []
        task.cancel()


# ── command ────────────────────────────────────────────────────────────────────


class TestCommandKind:
    async def test_fires_on_nonzero_exit(self) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            CommandKind.run(
                _defn("command", {"command": "exit 3", "interval_seconds": 60}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        # First tick runs immediately (real subprocess).
        await _poll_until(lambda: fired)
        assert fired[0]["returncode"] == 3
        task.cancel()

    async def test_baseline_then_output_change(self, tmp_path: Path) -> None:
        counter = tmp_path / "n"
        counter.write_text("1")
        cmd = f"cat {counter}"
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            CommandKind.run(
                _defn("command", {"command": cmd, "interval_seconds": 60}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        assert fired == []  # baseline established, no fire

        counter.write_text("2")
        clock.advance()  # tick 2: output changed
        await _poll_until(lambda: fired)
        assert len(fired) == 1
        assert "2" in fired[0]["output_tail"]
        task.cancel()

    async def test_no_fire_on_stable_success(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "n"
        sentinel.write_text("1")
        cmd = f"cat {sentinel}"
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            CommandKind.run(
                _defn("command", {"command": cmd, "interval_seconds": 60}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        clock.advance()
        await asyncio.sleep(0.1)  # give a spurious fire a chance to happen
        assert fired == []
        task.cancel()

    async def test_timeout_kills_process(self) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            CommandKind.run(
                _defn(
                    "command",
                    {
                        "command": "sleep 30",
                        "interval_seconds": 60,
                        "timeout_seconds": 0.3,
                    },
                ),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: fired)
        assert fired[0]["timed_out"] is True
        task.cancel()


# ── run_kind dispatch ──────────────────────────────────────────────────────────


class TestRunKind:
    async def test_dispatches_by_kind(self) -> None:
        clock = FakeClock()
        fired: list[dict] = []

        async def fire(name: str, kind: str, payload: dict) -> None:
            fired.append(payload)

        task = asyncio.create_task(
            run_kind(
                _defn("interval", {"seconds": 1, "once": True}),
                fire,
                sleep_fn=clock.sleep,
            )
        )
        await _poll_until(lambda: clock.has_pending_sleep)
        clock.advance()
        await asyncio.sleep(0)
        assert len(fired) == 1
        assert task.done()

    async def test_unknown_kind_raises(self) -> None:
        async def fire(name: str, kind: str, payload: dict) -> None:
            return None

        with pytest.raises(ValueError):
            await run_kind(_defn("ghost", {}), fire, sleep_fn=FakeClock().sleep)
