"""Unit tests for BgJobsPlugin: lifecycle, tools, wake path, /jobs.

Async tests use short real commands and temp dirs; no network. The commands go
through the platform shell, so they are built by the ``_echo_then_exit`` /
``_sleep`` helpers rather than written in POSIX syntax. Completion is observed
by polling the persistent wake queue, so the tests are deterministic without a
fake clock.
"""

import os
import ast
import asyncio
from typing import Any
from pathlib import Path

from phoson_agent import CliCommandInvocation
from phoson_plugin_bgjobs import BgJobsPlugin, render_wake_message
from phoson_plugin_bgjobs import _plugin as bgjobs_plugin
from phoson_agent.procutil import process_group_alive
from phoson_plugin_bgjobs.storage import JobDef, JobStore, WakeQueue

# ── shell-portable commands ────────────────────────────────────────────────────
# ``run_bg_job`` hands the command to the *platform* shell (``cmd.exe`` on
# Windows, ``/bin/sh`` elsewhere), so nothing here may assume POSIX syntax:
# ``sleep`` does not exist on Windows and ``;`` is not a separator there, so
# spelling these commands the POSIX way made the job exit immediately (and the
# test pass or fail depending on a scheduling race with the reaper).


def _echo_then_exit(text: str, code: int) -> str:
    """Print ``text`` and exit with ``code`` under either shell."""
    if os.name == "nt":
        return f"echo {text} & exit /b {code}"
    return f"echo {text}; exit {code}"


def _sleep(seconds: int) -> str:
    """A command that stays alive for about ``seconds`` under either shell."""
    if os.name == "nt":
        # ``ping`` is cmd.exe's cancelable wait (``timeout`` needs a console):
        # ``-n`` counts echoes, so the first one is immediate.
        return f"ping -n {seconds + 1} 127.0.0.1 >nul"
    return f"sleep {seconds}"


def _make_plugin(tmp_path: Path, **config) -> BgJobsPlugin:
    plugin = BgJobsPlugin()
    plugin.configure({"data_dir": str(tmp_path), **config})
    plugin.initialize()
    return plugin


def _tools(plugin: BgJobsPlugin) -> dict[str, Any]:
    return {t.name: t for t in plugin.get_tools()}


def _session_ctx(session_id: str = "sess-1") -> dict:
    return {"session_id_provider": lambda: session_id}


async def _wait_pending(plugin: BgJobsPlugin, n: int = 1, timeout: float = 10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while len(plugin.pending_wakes(None)) < n:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("wake did not arrive before timeout")
        await asyncio.sleep(0.02)
    return plugin.pending_wakes(None)


# ─ Tool schemas ──────────────────────────────────────────────────────────────


class TestToolSchemas:
    def test_names(self) -> None:
        names = [t.name for t in BgJobsPlugin().get_tools()]
        assert names == ["run_bg_job", "list_bg_jobs", "stop_bg_job", "wait_bg_jobs"]

    def test_run_schema(self) -> None:
        params = _tools(BgJobsPlugin())["run_bg_job"].parameters
        assert params["required"] == ["command"]
        # Injected kw-only parameter must NOT leak into the schema.
        assert "session_id_provider" not in params["properties"]

    def test_stop_schema(self) -> None:
        params = _tools(BgJobsPlugin())["stop_bg_job"].parameters
        assert params["required"] == ["job_id"]


# ── run / list / stop ──────────────────────────────────────────────────────────


class TestRunJob:
    async def test_run_returns_immediately_and_persists(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        result = await tools["run_bg_job"].handler(
            {"command": _sleep(5), "name": "long"}, _session_ctx()
        )
        assert "Background job" in result and "started" in result
        # The job is registered *before* it finishes (non-blocking).
        jobs = plugin._store.list()
        assert len(jobs) == 1
        assert jobs[0].name == "long" and jobs[0].state == "running"
        await tools["stop_bg_job"].handler({"job_id": jobs[0].job_id}, {})

    async def test_completion_wakes_with_exit_code_and_output(
        self, tmp_path: Path
    ) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        await tools["run_bg_job"].handler(
            {"command": _echo_then_exit("hello-bg", 3), "name": "demo"}, _session_ctx()
        )
        events = await _wait_pending(plugin)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["state"] == "completed"
        assert payload["returncode"] == 3
        assert "hello-bg" in payload["output_tail"]
        assert payload["duration_ms"] is not None
        assert events[0].session_id == "sess-1"

    async def test_on_wake_callback_invoked(self, tmp_path: Path) -> None:
        seen: list = []

        def on_wake(event) -> None:
            seen.append(event)

        plugin = _make_plugin(tmp_path, on_wake=on_wake)
        await _tools(plugin)["run_bg_job"].handler(
            {"command": _echo_then_exit("cb", 0), "name": "cb"}, _session_ctx()
        )
        await _wait_pending(plugin)
        deadline = asyncio.get_event_loop().time() + 5.0
        while not seen and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert seen and seen[0].job_id

    async def test_stop_kills_process_group(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        await tools["run_bg_job"].handler(
            {"command": _sleep(30), "name": "victim"}, _session_ctx()
        )
        job_id = plugin._store.list()[0].job_id
        result = await tools["stop_bg_job"].handler({"job_id": job_id}, {})
        assert "stopped" in result
        event = (await _wait_pending(plugin))[0]
        assert event.payload["state"] == "stopped"

    async def test_invalid_command_and_cwd_rejected(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        assert "Error" in await tools["run_bg_job"].handler({"command": "  "}, {})
        assert "Error" in await tools["run_bg_job"].handler(
            {"command": "true", "cwd": str(tmp_path / "nope")}, {}
        )

    async def test_list_reports_state(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        await tools["run_bg_job"].handler(
            {"command": "echo x", "name": "listed"}, _session_ctx()
        )
        await _wait_pending(plugin)
        listing = await tools["list_bg_jobs"].handler({}, {})
        assert "listed" in listing and "completed" in listing


# ── wake queue / rendering ─────────────────────────────────────────────────────


class TestWakes:
    async def test_drain_consumes(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        await _tools(plugin)["run_bg_job"].handler(
            {"command": "true", "name": "drain"}, _session_ctx()
        )
        await _wait_pending(plugin)
        drained = plugin.drain_pending_wakes("sess-1")
        assert len(drained) == 1
        assert plugin.pending_wakes("sess-1") == []

    async def test_render_message_shape(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        await _tools(plugin)["run_bg_job"].handler(
            {"command": _echo_then_exit("rendered", 2), "name": "render"},
            _session_ctx(),
        )
        events = await _wait_pending(plugin)
        events = plugin.drain_pending_wakes(None)
        text = render_wake_message(events)
        assert "[BACKGROUND JOB EVENTS]" in text
        assert "[render]" in text
        assert "exit 2" in text or "returncode: 2" in text

    def test_render_empty(self) -> None:
        assert render_wake_message([]) == ""


# ── crash recovery ─────────────────────────────────────────────────────────────


class TestRecovery:
    async def test_ensure_started_marks_dead_job_orphaned(self, tmp_path: Path) -> None:
        # Simulate a job left running on disk by a dead process.
        store = JobStore(tmp_path)
        from phoson_plugin_bgjobs.storage import JobDef

        store.add(
            JobDef(
                job_id="deadbeef",
                name="ghost",
                command="true",
                state="running",
                pid=999999,
                pgid=999999,
                session_id="s1",
            )
        )
        plugin = _make_plugin(tmp_path)
        await plugin.ensure_started()
        job = plugin._store.get("deadbeef")
        assert job is not None and job.state == "orphaned"
        assert len(plugin.pending_wakes(None)) == 1


# ── cross-platform process handling (#269) ─────────────────────────────────────


class TestCrossPlatformProcessHandling:
    """#269: ``os.killpg`` / ``os.getpgid`` are POSIX-only.

    On Windows the plugin must reconcile jobs (``ensure_started``) and launch
    new ones without touching those symbols — an unguarded call raised
    ``AttributeError`` and killed the plugin at startup. The primitives
    themselves live in :mod:`phoson_agent.procutil` (unit-tested separately);
    what the plugin owes is to keep going through them.
    """

    #: ``os.kill(pid, 0)`` is not a probe on Windows but ``TerminateProcess``,
    #: so a single reintroduced call can kill a recycled pid — a bluescreen
    #: when that pid belongs to a critical system process.
    _RAW_PRIMITIVES = frozenset({"kill", "killpg", "getpgid", "getpgrp", "setsid"})

    def test_never_calls_a_raw_kill_primitive(self) -> None:
        # Parsed rather than grepped: the prose in this module legitimately
        # *names* ``os.kill``, only an actual call is a regression. The module
        # itself must do this, because a compile-time import of ``os.killpg``
        # is what broke startup on Windows.
        tree = ast.parse(Path(bgjobs_plugin.__file__).read_text(encoding="utf-8"))
        raw = sorted(
            {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in self._RAW_PRIMITIVES
            }
        )
        assert raw == [], f"use phoson_agent.procutil instead of os.{raw}"

    def test_kill_process_sync_targets_the_recorded_group(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Stopping a job must reach the whole tree, not just the shell child
        # the plugin holds a handle to (which is all ``proc.kill()`` can do).
        plugin = _make_plugin(tmp_path)
        killed: list[int | None] = []
        monkeypatch.setattr(bgjobs_plugin, "kill_process_group", killed.append)

        class _ExitedChild:
            """A child that died between the wake and the stop."""

            def kill(self) -> None:
                raise ProcessLookupError

        job = JobDef(
            job_id="j1", name="n", command="c", state="running", pid=111, pgid=222
        )
        plugin._kill_process_sync(job, _ExitedChild())  # type: ignore[arg-type]
        assert killed == [222]

    async def test_launched_job_records_a_probeable_group(self, tmp_path: Path) -> None:
        # The handle stored by ``run_bg_job`` is what the reconciler probes
        # after a restart, and it must be the job's own — never our group,
        # which a group kill would take down with the host.
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        await tools["run_bg_job"].handler(
            {"command": _sleep(30), "name": "live"}, _session_ctx()
        )
        job = plugin._store.list()[0]
        assert job.pgid
        if os.name != "nt":
            assert job.pgid != os.getpgrp()
        assert process_group_alive(job.pgid) is True
        await tools["stop_bg_job"].handler({"job_id": job.job_id}, {})

    async def test_ensure_started_reconciles_dead_job(self, tmp_path: Path) -> None:
        # Regression: the startup reconciliation that crashed on Windows.
        JobStore(tmp_path).add(
            JobDef(
                job_id="deadbeef",
                name="ghost",
                command="whatever",
                state="running",
                pid=999_999_999,
                pgid=999_999_999,
                session_id="s1",
            )
        )
        plugin = _make_plugin(tmp_path)
        # Must not raise AttributeError on any platform.
        await plugin.ensure_started()
        job = plugin._store.get("deadbeef")
        assert job is not None and job.state == "orphaned"


# ── CLI extension: /jobs ───────────────────────────────────────────────────────


class TestJobsCommand:
    async def test_jobs_lists_without_crashing(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        published: list = []

        class _UI:
            def publish(self, block) -> None:
                published.append(block)

        notifications: list = []
        ctx = type(
            "Ctx",
            (),
            {
                "session_id": "s1",
                "ui": _UI(),
                "notify": staticmethod(
                    lambda kind, message: notifications.append((kind, message))
                ),
            },
        )()
        handled = await plugin.handle_jobs(
            CliCommandInvocation(name="/jobs", args=""), ctx
        )
        assert handled is True
        assert notifications


# ── storage ────────────────────────────────────────────────────────────────────


class TestStorage:
    def test_job_store_roundtrip(self, tmp_path: Path) -> None:
        from phoson_plugin_bgjobs.storage import JobDef

        store = JobStore(tmp_path)
        job = JobDef(job_id="a1", name="n", command="echo", state="running")
        store.add(job)
        assert store.get("a1") is not None
        # Reload from disk.
        assert JobStore(tmp_path).get("a1").command == "echo"
        assert store.remove("a1").job_id == "a1"
        assert store.get("a1") is None

    def test_wake_queue_dedupes_identical(self, tmp_path: Path) -> None:
        from phoson_plugin_bgjobs.storage import WakeEvent

        queue = WakeQueue(tmp_path, max_pending_per_job=5)
        first = queue.append(WakeEvent.create("j", "n", "s", {"returncode": 0}))
        second = queue.append(WakeEvent.create("j", "n", "s", {"returncode": 0}))
        assert first is not None and second is None
        assert queue.pending_count() == 1

    def test_wake_queue_cap(self, tmp_path: Path) -> None:
        from phoson_plugin_bgjobs.storage import WakeEvent

        queue = WakeQueue(tmp_path, max_pending_per_job=2)
        for i in range(4):
            queue.append(WakeEvent.create("j", "n", "s", {"i": i}))
        pending = queue.pending()
        assert len(pending) == 2
        assert pending[-1].payload["dropped_previous"] >= 1
