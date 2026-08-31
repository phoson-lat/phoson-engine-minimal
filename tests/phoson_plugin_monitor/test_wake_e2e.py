"""E2E test: the agent registers a monitor, it fires, and a host woken by
the wake resumes the same ConversationTree with the findings in context.

No real LLM (FakeToolChat), no real timers except one 1-second interval
monitor (integration test; skipped under --fast).
"""

import asyncio
from typing import Any
from pathlib import Path
from collections.abc import AsyncIterator

import pytest

from phoson_agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_agent.sessions import JsonlStorage, ConversationTree
from phoson_llm.chats.base import BaseLLMChat
from phoson_plugin_monitor import MonitorPlugin, render_wake_message
from phoson_plugin_monitor.storage import WakeQueue


def _is_fast() -> bool:
    import sys

    return "--fast" in sys.argv


pytestmark = pytest.mark.skipif(
    _is_fast(), reason="uses a real 1s timer (skipped with --fast)"
)


class _MonitorRegisterChat(BaseLLMChat):
    """Fake LLM: first call registers an interval monitor, then answers."""

    def __init__(self, seconds: float = 1.0) -> None:
        self._iteration = 0
        self.seconds = seconds
        self.tool_calls_seen: list[dict[str, Any]] = []

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self._iteration += 1
        yield LLMStartEvent(model=config.model, message_count=len(messages))

        if self._iteration == 1 and tools:
            tool_names = [t.name for t in tools]
            assert "register_monitor" in tool_names
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_register_1",
                tool_name="register_monitor",
                args={
                    "name": "build-watcher",
                    "kind": "interval",
                    "spec": {"seconds": self.seconds, "once": True},
                },
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=False,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return

        self.tool_calls_seen.append({"messages": list(messages)})
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=20, output=8),
            cost_usd=0.0,
            cost_known=False,
        )
        yield LLMDoneEvent(content="done", has_tool_calls=False)


class TestMonitorWakeE2E:
    async def test_agent_registers_monitor_and_host_resumes_tree(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "monitors"
        chat = _MonitorRegisterChat(seconds=1.0)
        plugin = MonitorPlugin()
        plugin.configure({"data_dir": str(data_dir)})
        plugin.initialize()

        # ── Run 1: the user asks the agent to watch for a build artifact. ──
        engine = AgentEngine(
            chat=chat,
            plugins=[plugin],
            max_iterations=2,
        )
        engine.context.extra["session_id_provider"] = lambda: "sess-e2e"

        storage = JsonlStorage(base_path=tmp_path / "sessions")
        tree = ConversationTree.new(session_id="sess-e2e")
        tree.append(None, Message(role="user", content="Watch for my build."))
        await storage.save(tree)

        result = await engine.run(
            tree.get_path(tree.get_leaves()[-1]),
            ModelConfig(model="fake-model"),
        )
        assert result.final_content == "done"

        # The fake LLM saw the monitor tool and called it; the monitor is
        # registered and running.
        monitor = plugin._store.get("build-watcher")
        assert monitor is not None
        assert monitor.session_id == "sess-e2e"
        assert "build-watcher" in plugin._tasks

        # ── The run ends; the host goes idle. The monitor fires once. ──
        await asyncio.sleep(1.4)
        queue = WakeQueue(data_dir)
        pending = queue.pending("sess-e2e")
        assert len(pending) == 1
        assert pending[0].monitor == "build-watcher"

        # ── Wake: the host drains the queue and resumes the same tree. ──
        drained = plugin.drain_pending_wakes("sess-e2e")
        assert len(drained) == 1
        wake_message = render_wake_message(drained)
        assert "[build-watcher]" in wake_message

        # The agent (host side) turns the findings into a new user turn on
        # the SAME conversation tree (session continuity).
        user_text = wake_message + "\n\nUser: did the build watcher fire?"
        node = tree.append(
            tree.get_leaves()[-1], Message(role="user", content=user_text)
        )
        await storage.save(tree)

        # A restarted host loads the identical tree and sees the wake in
        # context.
        reloaded_tree = await storage.load("sess-e2e")
        path = reloaded_tree.get_path(node.id)
        assert any("build-watcher" in str(m.content) for m in path)

        await plugin.aclose()

    async def test_wake_survives_process_restart(self, tmp_path: Path) -> None:
        """Crash between fire and drain: the wake must survive on disk."""
        data_dir = tmp_path / "monitors"
        plugin = MonitorPlugin()
        plugin.configure({"data_dir": str(data_dir)})
        plugin.initialize()
        await plugin._register_monitor(
            "crash-proof",
            "interval",
            {"seconds": 1, "once": True},
            lambda: "sess-restart",
        )
        await asyncio.sleep(1.4)
        await plugin.aclose()  # host dies

        # New process/host: the fire already happened (queue on disk), the
        # monitor's once=true completion was persisted as stopped.
        revived = MonitorPlugin()
        revived.configure({"data_dir": str(data_dir)})
        revived.initialize()
        pending = revived._queue.pending("sess-restart")
        assert len(pending) == 1
        assert revived._store.get("crash-proof").state == "stopped"
        await revived.aclose()
