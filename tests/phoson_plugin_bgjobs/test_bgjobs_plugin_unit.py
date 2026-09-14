"""Unit tests for BgJobsPlugin: lifecycle, tools, wake path, /jobs.

Async tests use short real commands (echo/exit/sleep) and temp dirs; no
network. Completion is observed by polling the persistent wake queue so the
tests are deterministic without a fake clock.
"""

import asyncio
from typing import Any
from pathlib import Path

from phoson_agent import CliCommandInvocation
from phoson_plugin_bgjobs import BgJobsPlugin, render_wake_message
from phoson_plugin_bgjobs.storage import JobStore, WakeQueue


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
            {"command": "sleep 5", "name": "long"}, _session_ctx()
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
            {"command": "echo hello-bg; exit 3", "name": "demo"}, _session_ctx()
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
            {"command": "true", "name": "cb"}, _session_ctx()
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
            {"command": "sleep 30", "name": "victim"}, _session_ctx()
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
            {"command": "echo rendered; exit 2", "name": "render"}, _session_ctx()
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
