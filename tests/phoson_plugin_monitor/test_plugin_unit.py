"""Unit tests for MonitorPlugin: lifecycle, tools, wake path, /monitors.

Async tests use short real timers where the point is persistence semantics;
everything else runs against temp dirs and fakes (no network).
"""

import os
import asyncio
from types import SimpleNamespace
from typing import Any
from pathlib import Path

from phoson_agent import CliCommandInvocation
from phoson_plugin_monitor import (
    MonitorPlugin,
    render_wake_message,
)
from phoson_plugin_monitor.storage import (
    WakeQueue,
    MonitorDef,
    monitor_key,
    normalize_cwd,
)


def _key(name: str) -> str:
    """Registry/task key of a monitor registered from the test's cwd."""
    return monitor_key(name, normalize_cwd(os.getcwd()))


def _make_plugin(tmp_path: Path, **config) -> MonitorPlugin:
    plugin = MonitorPlugin()
    plugin.configure({"data_dir": str(tmp_path), **config})
    plugin.initialize()
    return plugin


def _tools(plugin: MonitorPlugin) -> dict[str, Any]:
    return {t.name: t for t in plugin.get_tools()}


def _session_ctx(**kw):
    return {"session_id_provider": lambda: "sess-1", **kw}


# ── Tool schemas ───────────────────────────────────────────────────────────────


class TestToolSchemas:
    def test_names(self) -> None:
        names = [t.name for t in MonitorPlugin().get_tools()]
        assert names == ["register_monitor", "list_monitors", "stop_monitor"]

    def test_register_schema(self) -> None:
        t = _tools(MonitorPlugin())["register_monitor"]
        params = t.parameters
        assert params["required"] == ["name", "kind", "spec"]
        assert params["properties"]["kind"]["enum"] == [
            "interval",
            "file",
            "command",
        ]
        # Injected kw-only parameter must NOT leak into the schema.
        assert "session_id_provider" not in params["properties"]

    def test_list_has_no_params(self) -> None:
        t = _tools(MonitorPlugin())["list_monitors"]
        assert t.parameters["properties"] == {}
        assert "required" not in t.parameters

    def test_stop_schema(self) -> None:
        t = _tools(MonitorPlugin())["stop_monitor"]
        assert t.parameters["required"] == ["name"]


# ── register / list / stop ─────────────────────────────────────────────────────


class TestRegister:
    async def test_register_persists_and_starts_task(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            result = await _tools(plugin)["register_monitor"].handler(
                {"name": "build", "kind": "interval", "spec": {"seconds": 1}},
                _session_ctx(),
            )
            assert "registered" in result
            assert "sess-1" in result
            monitor = plugin._store.get(_key("build"))
            assert monitor is not None
            assert monitor.kind == "interval"
            assert monitor.spec == {"seconds": 1.0, "once": False}
            assert monitor.session_id == "sess-1"
            assert _key("build") in plugin._tasks
            # On-disk immediately (a crash right now must not lose it).
            reloaded = MonitorPlugin()
            reloaded.configure({"data_dir": str(tmp_path)})
            reloaded.initialize()
            assert reloaded._store.get(_key("build")) is not None
        finally:
            await plugin.aclose()

    async def test_register_invalid_name(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            result = await _tools(plugin)["register_monitor"].handler(
                {"name": "bad name!", "kind": "interval", "spec": {"seconds": 1}},
                _session_ctx(),
            )
            assert result.startswith("Error:")
        finally:
            await plugin.aclose()

    async def test_register_duplicate(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        try:
            await tools["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            result = await tools["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            assert "already exists" in result
        finally:
            await plugin.aclose()

    async def test_register_bad_kind_goes_to_llm_readable_error(
        self, tmp_path: Path
    ) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            result = await _tools(plugin)["register_monitor"].handler(
                {"name": "m", "kind": "http", "spec": {}},
                _session_ctx(),
            )
            assert result.startswith("Error:")
            assert "Unknown monitor kind" in result
        finally:
            await plugin.aclose()

    async def test_register_bad_spec_fields(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            result = await _tools(plugin)["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 0}},
                _session_ctx(),
            )
            assert result.startswith("Error:")
            assert "spec.seconds" in result
        finally:
            await plugin.aclose()

    async def test_register_without_session_provider(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path, default_session_id="fallback-sess")
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                {},
            )
            assert plugin._store.get(_key("m")).session_id == "fallback-sess"
        finally:
            await plugin.aclose()

    async def test_stop_monitor_cancels_and_removes(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        tools = _tools(plugin)
        try:
            await tools["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            assert _key("m") in plugin._tasks
            result = await tools["stop_monitor"].handler({"name": "m"})
            assert "stopped" in result
            assert plugin._store.get(_key("m")) is None
            # Re-registering the same name works again.
            await tools["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            assert plugin._store.get(_key("m")) is not None
        finally:
            await plugin.aclose()

    async def test_stop_unknown(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            result = await _tools(plugin)["stop_monitor"].handler({"name": "ghost"})
            assert result.startswith("Error:")
        finally:
            await plugin.aclose()

    async def test_list_monitors_empty(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            result = await _tools(plugin)["list_monitors"].handler({})
            assert result == "No monitors registered in this working directory."
        finally:
            await plugin.aclose()

    async def test_list_monitors_shows_state(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            result = await _tools(plugin)["list_monitors"].handler({})
            assert "m [interval] running" in result
        finally:
            await plugin.aclose()


# ── monitor_status() host hook ─────────────────────────────────────────────────


class TestMonitorStatus:
    async def test_none_when_no_monitors(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            assert plugin.monitor_status() is None
        finally:
            await plugin.aclose()

    async def test_lists_running_monitors(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "heartbeat", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            status = plugin.monitor_status()
            assert status is not None
            assert "heartbeat" in status
            assert "⏳" in status
        finally:
            await plugin.aclose()

    async def test_truncates_long_names(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            register = _tools(plugin)["register_monitor"].handler
            for i in range(6):
                await register(
                    {"name": f"m{i}", "kind": "interval", "spec": {"seconds": 5}},
                    _session_ctx(),
                )
            status = plugin.monitor_status()
            assert status is not None
            assert "+2" in status  # 6 registered, 4 shown, +2 overflow
        finally:
            await plugin.aclose()

    async def test_excludes_stopped(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "a", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            await _tools(plugin)["stop_monitor"].handler({"name": "a"}, _session_ctx())
            assert plugin.monitor_status() is None
        finally:
            await plugin.aclose()


# ── Wake path ──────────────────────────────────────────────────────────────────


class TestWakePath:
    async def test_fire_persists_and_calls_on_wake(self, tmp_path: Path) -> None:
        received: list = []
        plugin = _make_plugin(tmp_path, on_wake=lambda ev: received.append(ev))
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "tick", "kind": "interval", "spec": {"seconds": 1}},
                _session_ctx(),
            )
            await asyncio.sleep(1.3)
            # Exactly one fire, one callback.
            assert len(received) == 1
            assert received[0].session_id == "sess-1"
            assert received[0].kind == "interval"
            # Persisted with the original session id.
            queue = WakeQueue(tmp_path)
            pending = queue.pending("sess-1")
            assert len(pending) == 1
            assert pending[0].monitor == "tick"
            # Drain marks it consumed on disk.
            drained = plugin.drain_pending_wakes("sess-1")
            assert len(drained) == 1
            assert plugin.drain_pending_wakes("sess-1") == []
            message = render_wake_message(drained)
            assert "[MONITOR EVENTS]" in message
            assert "tick" in message
        finally:
            await plugin.aclose()

    async def test_fire_not_consumed_by_other_session(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "tick", "kind": "interval", "spec": {"seconds": 1}},
                _session_ctx(),
            )
            await asyncio.sleep(1.3)
            assert plugin.drain_pending_wakes("someone-else") == []
            assert len(plugin.drain_pending_wakes("sess-1")) == 1
        finally:
            await plugin.aclose()

    async def test_once_monitor_stops_after_fire(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {
                    "name": "once",
                    "kind": "interval",
                    "spec": {"seconds": 1, "once": True},
                },
                _session_ctx(),
            )
            await asyncio.sleep(1.3)
            monitor = plugin._store.get(_key("once"))
            assert monitor is not None
            assert monitor.state == "stopped"
            assert _key("once") not in plugin._tasks
            assert len(plugin.drain_pending_wakes("sess-1")) == 1
        finally:
            await plugin.aclose()

    async def test_on_wake_failure_does_not_lose_event(self, tmp_path: Path) -> None:
        def broken(_event) -> None:
            raise RuntimeError("host callback exploded")

        plugin = _make_plugin(tmp_path, on_wake=broken)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "tick", "kind": "interval", "spec": {"seconds": 1}},
                _session_ctx(),
            )
            await asyncio.sleep(1.3)
            # The queue is the source of truth: event still there.
            assert len(plugin.drain_pending_wakes("sess-1")) == 1
        finally:
            await plugin.aclose()

    async def test_aclose_cancels_tasks_keeps_state_running(
        self, tmp_path: Path
    ) -> None:
        plugin = _make_plugin(tmp_path)
        await _tools(plugin)["register_monitor"].handler(
            {"name": "long", "kind": "interval", "spec": {"seconds": 60}},
            _session_ctx(),
        )
        assert any(not t.done() for t in plugin._tasks.values())
        await plugin.aclose()
        assert all(t.done() for t in plugin._tasks.values())
        # State on disk is untouched: the next host resurrects it.
        reloaded = MonitorPlugin()
        reloaded.configure({"data_dir": str(tmp_path)})
        reloaded.initialize()
        assert reloaded._store.get(_key("long")).state == "running"

    async def test_resurrection_after_restart(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        await _tools(plugin)["register_monitor"].handler(
            {"name": "long", "kind": "interval", "spec": {"seconds": 60}},
            _session_ctx(),
        )
        await plugin.aclose()

        # Fresh host (new loop, new instance) picks the monitor back up.
        revived = MonitorPlugin()
        revived.configure({"data_dir": str(tmp_path)})
        revived.initialize()
        await revived.ensure_started()
        assert _key("long") in revived._tasks
        assert not revived._tasks[_key("long")].done()
        await revived.aclose()

    async def test_ensure_started_is_idempotent(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        await _tools(plugin)["register_monitor"].handler(
            {"name": "m", "kind": "interval", "spec": {"seconds": 60}},
            _session_ctx(),
        )
        first_task = plugin._tasks[_key("m")]
        await plugin.ensure_started()
        assert plugin._tasks[_key("m")] is first_task
        await plugin.aclose()

    async def test_double_configure_does_not_reset(self, tmp_path: Path) -> None:
        # The loader re-runs configure() with {} on engine rebuild.
        plugin = _make_plugin(tmp_path, max_pending_wakes=9)
        plugin.configure({})
        assert plugin._max_pending_wakes == 9
        assert plugin._store is not None
        await plugin.aclose()


# ── /monitors command ──────────────────────────────────────────────────────────


class _FakeUi:
    def __init__(self) -> None:
        self.blocks: list = []

    def publish(self, block: Any) -> None:
        self.blocks.append(block)

    def replace(self, block_id: str, block: Any) -> None:
        self.blocks.append(block)

    def remove(self, block_id: str) -> None:
        self.blocks = []


def _cmd_context(tmp_path: Path, ui: _FakeUi, **kw) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=kw.get("session_id", "sess-1"),
        ui=ui,
        cwd=Path(tmp_path),
        plugin_name="phoson-plugin-monitor",
        notify=lambda kind, message: _NOTIFIES.append((kind, message)),
    )


_NOTIFIES: list[tuple[str, str]] = []


class TestMonitorsCommand:
    def test_spec_declares_handler(self) -> None:
        commands = MonitorPlugin().get_commands()
        assert len(commands) == 1
        assert commands[0].names == ("/monitors",)
        assert commands[0].handler == "handle_monitors"
        assert commands[0].primary == "/monitors"

    async def test_lists_all(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        ui = _FakeUi()
        _NOTIFIES.clear()
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "m1", "kind": "interval", "spec": {"seconds": 60}},
                _session_ctx(),
            )
            result = await plugin.handle_monitors(
                CliCommandInvocation(name="/monitors", args=""),
                _cmd_context(tmp_path, ui),
            )
            assert result is True
            items = [item for block in ui.blocks for item in block.items]
            assert ("m1", "running · interval") in items
        finally:
            await plugin.aclose()

    async def test_lists_one(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        ui = _FakeUi()
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "m1", "kind": "interval", "spec": {"seconds": 60}},
                _session_ctx(),
            )
            result = await plugin.handle_monitors(
                CliCommandInvocation(name="/monitors", args="m1"),
                _cmd_context(tmp_path, ui),
            )
            assert result is True
            listed = [item for block in ui.blocks for item in block.items]
            names = [item[0] for item in listed if item[0]]
            assert names == ["m1"]
        finally:
            await plugin.aclose()

    async def test_unknown_name_notifies(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        _NOTIFIES.clear()
        try:
            result = await plugin.handle_monitors(
                CliCommandInvocation(name="/monitors", args="ghost"),
                _cmd_context(tmp_path, _FakeUi()),
            )
            assert result is True
            assert any("No monitor named" in msg for _, msg in _NOTIFIES)
        finally:
            await plugin.aclose()

    async def test_pending_subcommand(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        _NOTIFIES.clear()
        try:
            await plugin.handle_monitors(
                CliCommandInvocation(name="/monitors", args="pending"),
                _cmd_context(tmp_path, _FakeUi()),
            )
            assert result_ok(_NOTIFIES)
        finally:
            await plugin.aclose()


def result_ok(notifies: list[tuple[str, str]]) -> bool:
    return any("pending monitor wake" in msg for _, msg in notifies)


# ── render_wake_message ───────────────────────────────────────────────────────


class TestRenderWakeMessage:
    def test_empty(self) -> None:
        assert render_wake_message([]) == ""

    def test_formats_payload(self, tmp_path: Path) -> None:
        from phoson_plugin_monitor.storage import WakeEvent

        event = WakeEvent.create(
            "watcher",
            "file",
            "sess-9",
            {"changed": ["a.log"], "note": "hello"},
        )
        message = render_wake_message([event])
        assert "[MONITOR EVENTS]" in message
        assert "[watcher] kind=file" in message
        assert 'changed: ["a.log"]' in message
        assert "note: hello" in message


# ── Working-directory scope ───────────────────────────────────────────────────


class _Scope:
    """Mutable cwd provider: lets a test move the host between directories."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = str(cwd)

    def __call__(self) -> str:
        return self.cwd


def _scoped(tmp_path: Path, scope: _Scope) -> MonitorPlugin:
    return _make_plugin(tmp_path / "state", cwd_provider=scope)


class TestCwdScope:
    async def test_monitors_are_isolated_per_directory(self, tmp_path: Path) -> None:
        dir_a, dir_b = tmp_path / "a", tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        scope = _Scope(dir_a)
        plugin = _scoped(tmp_path, scope)
        tools = _tools(plugin)
        spec = {"name": "watch", "kind": "interval", "spec": {"seconds": 60}}
        try:
            assert "registered" in await tools["register_monitor"].handler(
                spec, _session_ctx()
            )
            scope.cwd = str(dir_b)
            listing = await tools["list_monitors"].handler({}, {})
            assert "watch" not in listing
            assert plugin.monitor_status() is None
            # Same name is free in B: names are unique per directory.
            assert "registered" in await tools["register_monitor"].handler(
                spec, _session_ctx()
            )
            assert "stopped" in await tools["stop_monitor"].handler(
                {"name": "watch"}, {}
            )
            scope.cwd = str(dir_a)
            listing = await tools["list_monitors"].handler({}, {})
            assert "watch" in listing  # B's stop never touched A's monitor
            assert plugin._store.get(monitor_key("watch", normalize_cwd(dir_a)))
        finally:
            await plugin.aclose()

    async def test_reconcile_follows_directory_change(self, tmp_path: Path) -> None:
        dir_a, dir_b = tmp_path / "a", tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        scope = _Scope(dir_a)
        plugin = _scoped(tmp_path, scope)
        key = monitor_key("long", normalize_cwd(dir_a))
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "long", "kind": "interval", "spec": {"seconds": 60}},
                _session_ctx(),
            )
            task = plugin._tasks[key]
            scope.cwd = str(dir_b)
            await plugin.reconcile_scope()
            assert key not in plugin._tasks and task.cancelled()
            # Leaving the directory pauses the watcher; disk keeps it running.
            assert plugin._store.get(key).state == "running"
            scope.cwd = str(dir_a)
            await plugin.reconcile_scope()
            assert not plugin._tasks[key].done()
        finally:
            await plugin.aclose()

    async def test_registration_uses_tool_context_cwd(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path / "state")
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 60}},
                _session_ctx(cwd_provider=lambda: str(tmp_path)),
            )
            monitor = plugin._store.get(monitor_key("m", normalize_cwd(tmp_path)))
            assert monitor is not None and monitor.cwd == normalize_cwd(tmp_path)
        finally:
            await plugin.aclose()

    async def test_legacy_unscoped_monitor_visible_everywhere(
        self, tmp_path: Path
    ) -> None:
        scope = _Scope(tmp_path)
        plugin = _scoped(tmp_path, scope)
        plugin._store.add(
            MonitorDef(name="old", kind="interval", spec={"seconds": 60.0})
        )
        try:
            assert "old" in await _tools(plugin)["list_monitors"].handler({}, {})
            assert "stopped" in await _tools(plugin)["stop_monitor"].handler(
                {"name": "old"}, {}
            )
        finally:
            await plugin.aclose()

    async def test_fire_is_tagged_with_cwd(self, tmp_path: Path) -> None:
        scope = _Scope(tmp_path)
        plugin = _scoped(tmp_path, scope)
        try:
            await _tools(plugin)["register_monitor"].handler(
                {"name": "tick", "kind": "interval", "spec": {"seconds": 60}},
                _session_ctx(),
            )
            key = monitor_key("tick", normalize_cwd(tmp_path))
            await plugin._fire(key, "interval", {"n": 1})
            (event,) = plugin.pending_wakes("sess-1")
            assert event.monitor == "tick" and event.cwd == normalize_cwd(tmp_path)
            assert plugin._store.get(key).cwd == normalize_cwd(tmp_path)
            assert plugin._store.get(key).fire_count == 1
        finally:
            await plugin.aclose()
