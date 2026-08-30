"""Unit tests for MonitorPlugin: lifecycle, tools, wake path, /monitors.

Async tests use short real timers where the point is persistence semantics;
everything else runs against temp dirs and fakes (no network).
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from pathlib import Path

from phoson_agent import CliCommandInvocation
from phoson_plugin_monitor import (
    MonitorPlugin,
    render_wake_message,
)
from phoson_plugin_monitor.storage import WakeQueue


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
            monitor = plugin._store.get("build")
            assert monitor is not None
            assert monitor.kind == "interval"
            assert monitor.spec == {"seconds": 1.0, "once": False}
            assert monitor.session_id == "sess-1"
            assert "build" in plugin._tasks
            # On-disk immediately (a crash right now must not lose it).
            reloaded = MonitorPlugin()
            reloaded.configure({"data_dir": str(tmp_path)})
            reloaded.initialize()
            assert reloaded._store.get("build") is not None
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
            assert plugin._store.get("m").session_id == "fallback-sess"
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
            assert "m" in plugin._tasks
            result = await tools["stop_monitor"].handler({"name": "m"})
            assert "stopped" in result
            assert plugin._store.get("m") is None
            # Re-registering the same name works again.
            await tools["register_monitor"].handler(
                {"name": "m", "kind": "interval", "spec": {"seconds": 5}},
                _session_ctx(),
            )
            assert plugin._store.get("m") is not None
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
            assert result == "No monitors registered."
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
            monitor = plugin._store.get("once")
            assert monitor is not None
            assert monitor.state == "stopped"
            assert "once" not in plugin._tasks
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
        assert reloaded._store.get("long").state == "running"

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
        assert "long" in revived._tasks
        assert not revived._tasks["long"].done()
        await revived.aclose()

    async def test_ensure_started_is_idempotent(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path)
        await _tools(plugin)["register_monitor"].handler(
            {"name": "m", "kind": "interval", "spec": {"seconds": 60}},
            _session_ctx(),
        )
        first_task = plugin._tasks["m"]
        await plugin.ensure_started()
        assert plugin._tasks["m"] is first_task
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
