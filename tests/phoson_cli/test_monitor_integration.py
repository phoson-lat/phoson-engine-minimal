"""CLI integration tests for the monitor plugin (I-126).

Covers the host-side glue: config opt-in, plugin spec building,
session_id_provider injection, wake draining into the next user turn,
and resurrection across engine rebuilds.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from phoson_agent import Plugin
from phoson_cli.config import PhosonConfig, load_config
from phoson_llm.schemas import Message
from phoson_agent.models import AgentDoneEvent, AgentRunResult, AgentStartEvent
from phoson_cli.controller import SessionController
from phoson_plugin_monitor import MonitorPlugin
from phoson_cli.session_utils import (
    drain_monitor_wakes,
    find_monitor_plugin,
    build_monitor_plugins,
)
from phoson_plugin_monitor.storage import WakeEvent


def _done_event(answer="hello") -> AgentDoneEvent:
    return AgentDoneEvent(
        result=AgentRunResult(
            final_content=answer,
            history=[
                Message(role="user", content="q"),
                Message(role="assistant", content=answer),
            ],
            input_messages=[],
            steps=[],
        )
    )


class FakeSink:
    """Recording AgentEventSink (mirrors test_controller_unit.FakeSink)."""

    def __init__(self) -> None:
        self.events: list = []
        self.user_messages: list[tuple[str, Message]] = []
        self.attachments: list[list[str]] = []
        self.notifications: list[tuple[str, str]] = []
        self.session_ids: list[str] = []
        self.history_calls: list[tuple[list[Message], int]] = []
        self.reasoning = ""
        self.partial_captures = 0
        self.flushes = 0
        self.subagent_progress_events: list = []

    def on_user_message(self, text, message) -> None:
        self.user_messages.append((text, message))

    def on_attachments(self, sources) -> None:
        self.attachments.append(list(sources))

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_subagent_progress(self, progress) -> None:
        self.subagent_progress_events.append(progress)

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def set_session(self, session_id) -> None:
        self.session_ids.append(session_id)

    def take_reasoning(self) -> str:
        r, self.reasoning = self.reasoning, ""
        return r

    def flush_line(self) -> None:
        self.flushes += 1

    def capture_partial_reasoning(self) -> None:
        self.partial_captures += 1

    def print_history(self, path, tail=None) -> None:
        self.history_calls.append((path, tail))


def _make_controller(tmp_path, **cfg) -> tuple[SessionController, FakeSink]:
    sink = FakeSink()
    config = PhosonConfig(
        provider="ollama", model="test-model", sessions_dir=tmp_path, **cfg
    )
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)
    return controller, sink


def _fake_stream(events):
    async def stream(path, config):
        for event in events:
            yield event

    return stream


# ── config ─────────────────────────────────────────────────────────────────────


def _isolated_home(tmp_path: Path, monkeypatch, toml_body: str = "") -> Path:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "PHOSON_ENABLE_MONITORS",
        "PHOSON_MONITORS_DIR",
        "PHOSON_MODEL",
        "PHOSON_PROVIDER",
        "PHOSON_SESSIONS_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    if toml_body:
        (config_dir / "config.toml").write_text(toml_body, encoding="utf-8")
    return home


class TestConfig:
    def test_default_disabled(self) -> None:
        config = PhosonConfig()
        assert config.enable_monitors is False
        assert config.monitors_data_dir == Path("~/.phoson/monitors/").expanduser()
        assert build_monitor_plugins(config) == []

    def test_env_override(self, tmp_path, monkeypatch) -> None:
        _isolated_home(tmp_path, monkeypatch)
        monkeypatch.setenv("PHOSON_ENABLE_MONITORS", "1")
        config = load_config()
        assert config.enable_monitors is True

    def test_toml_override(self, tmp_path, monkeypatch) -> None:
        _isolated_home(
            tmp_path,
            monkeypatch,
            "[defaults]\nprovider = 'ollama'\nenable_monitors = true\n",
        )
        config = load_config()
        assert config.enable_monitors is True


# ── spec building ──────────────────────────────────────────────────────────────


class TestBuildMonitorPlugins:
    def test_disabled_returns_empty(self, tmp_path) -> None:
        config = PhosonConfig(
            provider="ollama",
            model="m",
            enable_monitors=False,
            monitors_data_dir=tmp_path,
        )
        assert build_monitor_plugins(config) == []

    def test_enabled_returns_preconfigured_instance(self, tmp_path) -> None:
        config = PhosonConfig(
            provider="ollama",
            model="m",
            enable_monitors=True,
            monitors_data_dir=tmp_path / "mon",
        )
        specs = build_monitor_plugins(config)
        assert len(specs) == 1
        assert isinstance(specs[0], MonitorPlugin)
        assert specs[0]._data_dir == str(tmp_path / "mon")
        # Each call yields a fresh instance (no shared singleton state).
        assert build_monitor_plugins(config)[0] is not specs[0]


# ── controller wiring ──────────────────────────────────────────────────────────


class TestControllerWiring:
    def test_injects_session_id_provider(self, tmp_path) -> None:
        controller, _ = _make_controller(
            tmp_path,
            enable_monitors=True,
            monitors_data_dir=tmp_path / "mon",
        )
        provider = controller.engine.context.extra.get("session_id_provider")
        assert callable(provider)
        original = controller.tree.session_id
        assert provider() == original

        # new_session swaps the tree; the provider follows (no rebuild).
        controller.new_session()
        assert provider() != original
        assert provider() == controller.tree.session_id

    async def test_ensure_started_runs_on_rebuild(self, tmp_path) -> None:
        """Engine rebuilds (/model etc.) resurrect running monitors."""
        data_dir = tmp_path / "mon"
        controller, _ = _make_controller(
            tmp_path,
            enable_monitors=True,
            monitors_data_dir=data_dir,
        )
        plugin = find_monitor_plugin(controller.engine._loaded_plugins)
        assert plugin is not None

        # Register a monitor through the real tool handler.
        tools = {t.name: t for t in controller.engine.tools}
        result = await tools["register_monitor"].handler(
            {"name": "long", "kind": "interval", "spec": {"seconds": 60}},
            {
                "session_id_provider": controller.engine.context.extra[
                    "session_id_provider"
                ]
            },
        )
        assert "registered" in result
        assert "long" in plugin._tasks

        # Rebuild the engine (what /model does): old plugin is closed and
        # a new one must resurrect the monitor from disk.
        controller._rebuild_engine()
        await asyncio.sleep(0)  # let the ensure_started task run
        new_plugin = find_monitor_plugin(controller.engine._loaded_plugins)
        assert new_plugin is not plugin
        assert "long" in new_plugin._tasks
        assert not new_plugin._tasks["long"].done()

        await controller.shutdown()

    async def test_run_turn_drains_wakes_into_user_message(self, tmp_path) -> None:
        controller, sink = _make_controller(
            tmp_path,
            enable_monitors=True,
            monitors_data_dir=tmp_path / "mon",
        )
        plugin = find_monitor_plugin(controller.engine._loaded_plugins)
        assert plugin is not None

        # A monitor fired while the user was away: queue one wake.
        session_id = controller.tree.session_id
        plugin._queue.append(
            WakeEvent.create(
                "build-watcher",
                "file",
                session_id,
                {"changed": ["dist/app.bin"]},
            )
        )

        controller.engine.stream = _fake_stream(
            [
                AgentStartEvent(model="m", message_count=1, max_iterations=50),
                _done_event("ok"),
            ]
        )
        outcome = await controller.run_turn("did the build finish?")
        assert outcome.status == "done"

        # The model's user message carries the wake header + the input.
        text, message = sink.user_messages[0]
        assert "[MONITOR EVENTS]" in text
        assert "build-watcher" in text
        assert "did the build finish?" in text
        # Content is a list of blocks; flatten to text to assert.
        content = message.content
        content_text = (
            content if isinstance(content, str) else "".join(b.text for b in content)
        )
        assert "[MONITOR EVENTS]" in content_text
        assert "dist/app.bin" in content_text

        # The sink told the user what happened.
        assert any("1 monitor wake" in msg for _, msg in sink.notifications)

        # The queue is consumed: a second turn delivers nothing.
        plugin._queue.append(WakeEvent.create("x", "interval", "other-session", {}))
        controller.engine.stream = _fake_stream(
            [
                AgentStartEvent(model="m", message_count=1, max_iterations=50),
                _done_event("ok"),
            ]
        )
        await controller.run_turn("next")
        text2, _ = sink.user_messages[1]
        assert "[MONITOR EVENTS]" not in text2

        await controller.shutdown()

    async def test_run_turn_without_wakes_is_noop(self, tmp_path) -> None:
        controller, sink = _make_controller(
            tmp_path,
            enable_monitors=True,
            monitors_data_dir=tmp_path / "mon",
        )
        controller.engine.stream = _fake_stream(
            [
                AgentStartEvent(model="m", message_count=1, max_iterations=50),
                _done_event("ok"),
            ]
        )
        await controller.run_turn("plain question")
        text, _ = sink.user_messages[0]
        assert text == "plain question"
        assert not any("monitor" in msg.lower() for _, msg in sink.notifications)
        await controller.shutdown()

    def test_disabled_plugin_absent(self, tmp_path) -> None:
        controller, _ = _make_controller(tmp_path)
        assert find_monitor_plugin(controller.engine._loaded_plugins) is None
        assert "register_monitor" not in {t.name for t in controller.engine.tools}


# ── session_utils helpers ──────────────────────────────────────────────────────


class TestDrainHelper:
    async def test_none_plugin_returns_empty(self) -> None:
        assert await drain_monitor_wakes(None, "s") == []

    async def test_broken_plugin_never_raises(self) -> None:
        class Broken(Plugin):
            @property
            def name(self) -> str:
                return "broken"

            def drain_pending_wakes(self, session_id):
                raise RuntimeError("queue corrupt")

        assert await drain_monitor_wakes(Broken(), "s") == []
