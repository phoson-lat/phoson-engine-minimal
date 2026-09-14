"""Multi-provider wake integration (option B of #217).

The CLI must compose *every* wake provider (monitors, background jobs, ...)
instead of only the first one. These tests cover the session_utils helpers
and the controller end-to-end with both plugins enabled.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from phoson_agent import Plugin
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_agent.models import AgentDoneEvent, AgentRunResult, AgentStartEvent
from phoson_plugin_bgjobs import BgJobsPlugin
from phoson_cli.controller import SessionController
from phoson_plugin_monitor import MonitorPlugin
from phoson_cli.session_utils import (
    drain_all_wakes,
    find_wake_plugins,
    has_pending_wakes,
)
from phoson_plugin_bgjobs.storage import WakeEvent as JobWake
from phoson_plugin_monitor.storage import WakeEvent as MonitorWake


class _FakeProvider(Plugin):
    def __init__(self, name: str, events: list) -> None:
        self._name = name
        self._events = events

    @property
    def name(self) -> str:
        return self._name

    def pending_wakes(self, session_id):
        return list(self._events)

    def drain_pending_wakes(self, session_id):
        drained, self._events = self._events, []
        return drained

    def render_wake_message(self, events):
        return f"[EVENTS from {self._name}] " + ", ".join(str(e) for e in events)


class TestHelpers:
    def test_find_wake_plugins_returns_all(self) -> None:
        a = _FakeProvider("a", [])
        b = _FakeProvider("b", [])
        plain = MagicMock(spec=Plugin)
        del plain.drain_pending_wakes
        found = find_wake_plugins([a, plain, b])
        assert found == [a, b]

    def test_has_pending_wakes_any_provider(self) -> None:
        empty = _FakeProvider("empty", [])
        full = _FakeProvider("full", ["x"])
        assert has_pending_wakes([empty, full], "s") is True
        assert has_pending_wakes([empty], "s") is False

    async def test_drain_all_composes_both(self) -> None:
        a = _FakeProvider("a", ["a1"])
        b = _FakeProvider("b", ["b1", "b2"])
        batches = await drain_all_wakes([a, b], "s")
        assert [(p.name, len(evs)) for p, evs in batches] == [("a", 1), ("b", 2)]
        # Both were consumed.
        assert await drain_all_wakes([a, b], "s") == []


# ── controller end-to-end ──────────────────────────────────────────────────────


class _FakeSink:
    def __init__(self) -> None:
        self.user_messages: list = []
        self.notifications: list = []

    def on_user_message(self, text, message) -> None:
        self.user_messages.append((text, message))

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def __getattr__(self, _name):
        # Any other sink hook is a no-op.
        return lambda *a, **k: None


def _done_event(answer: str = "ok") -> AgentDoneEvent:
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


def _fake_stream(events):
    async def stream(path, config):
        for event in events:
            yield event

    return stream


def _make_controller(tmp_path) -> tuple[SessionController, _FakeSink]:
    sink = _FakeSink()
    config = PhosonConfig(
        provider="ollama",
        model="test-model",
        sessions_dir=tmp_path,
        enable_monitors=True,
        monitors_data_dir=tmp_path / "mon",
        enable_bgjobs=True,
        bgjobs_data_dir=tmp_path / "bg",
    )
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)
    return controller, sink


class TestControllerComposesProviders:
    async def test_run_turn_delivers_both_headers(self, tmp_path) -> None:
        controller, sink = _make_controller(tmp_path)
        plugins = find_wake_plugins(controller.engine._loaded_plugins)
        kinds = {type(p).__name__ for p in plugins}
        assert {"MonitorPlugin", "BgJobsPlugin"} <= kinds

        session_id = controller.tree.session_id
        monitor = next(p for p in plugins if isinstance(p, MonitorPlugin))
        bgjobs = next(p for p in plugins if isinstance(p, BgJobsPlugin))
        monitor._queue.append(
            MonitorWake.create("watcher", "file", session_id, {"changed": ["a"]})
        )
        bgjobs._queue.append(
            JobWake.create("j1", "build", session_id, {"returncode": 0})
        )

        controller.engine.stream = _fake_stream(
            [
                AgentStartEvent(model="m", message_count=1, max_iterations=50),
                _done_event(),
            ]
        )
        outcome = await controller.run_turn("status?")
        assert outcome.status == "done"

        text, _message = sink.user_messages[0]
        assert "[MONITOR EVENTS]" in text
        assert "[BACKGROUND JOB EVENTS]" in text
        assert "status?" in text
        # One consolidated notification, and both queues drained.
        assert any("2 wake" in msg for _, msg in sink.notifications)
        assert monitor._queue.pending() == []
        assert bgjobs._queue.pending() == []
        await controller.shutdown()

    async def test_bgjobs_wake_triggers_autonomous_turn(self, tmp_path) -> None:
        controller, _sink = _make_controller(tmp_path)
        plugins = find_wake_plugins(controller.engine._loaded_plugins)
        bgjobs = next(p for p in plugins if isinstance(p, BgJobsPlugin))
        bgjobs._queue.append(
            JobWake.create(
                "j2", "deploy", controller.tree.session_id, {"returncode": 0}
            )
        )

        paths: list = []

        async def stream(path, config):
            paths.append(path)
            yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
            yield _done_event()

        controller.engine.stream = stream
        controller._wake_poll_seconds = 0.02
        controller.start_monitor_wake_loop()
        try:
            import asyncio

            for _ in range(200):
                if paths:
                    break
                await asyncio.sleep(0.02)
            assert paths, "bgjobs wake did not trigger an autonomous turn"
            content = paths[-1][-1].content
            text = (
                content
                if isinstance(content, str)
                else "".join(b.text for b in content)
            )
            assert "[BACKGROUND JOB EVENTS]" in text
            assert bgjobs._queue.pending() == []
        finally:
            await controller.shutdown()
