"""Standalone host example for the monitor plugin (I-126).

Demonstrates the full wake loop **without the CLI**: the host keeps one
`ConversationTree` (persisted via `JsonlStorage`), the agent registers a
monitor during a run, and when the monitor fires the host's `on_wake`
callback starts a *new* run that **resumes the same conversation tree**
with the findings as new input.

Run (needs a configured provider in ~/.phoson/config.toml):

    python examples/monitor_wake_host.py

Press Ctrl+C twice to exit. Watch `~/.phoson/monitors/` while it runs:
`monitors.json` holds the registry and `wake.jsonl` the fire queue.

The Phoson CLI ships the same integration out of the box (opt-in with
`enable_monitors = true`); this example exists to show how a custom
embedded host (e.g. Phoson-Core) wires the wake channel itself.
"""

import sys
import asyncio
from pathlib import Path

# Make the in-tree package importable when running from a source checkout
# without installing it (the plugin ships in the wheel, so installed
# environments work either way).
sys.path.insert(0, str(Path(__file__).parent.parent))

from phoson_agent import AgentEngine
from phoson_cli.config import load_config
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.sessions import JsonlStorage, ConversationTree
from phoson_plugin_monitor import MonitorPlugin, render_wake_message


class WakeHost:
    """A minimal embedded host: one engine, one tree, one wake channel."""

    def __init__(self, chat, config, data_dir: Path, sessions_dir: Path) -> None:
        self.chat = chat
        self.config = config
        self.sessions_dir = sessions_dir
        self.storage = JsonlStorage(base_path=sessions_dir)
        self.tree = ConversationTree.new(session_id="demo-monitor-session")
        self.plugin = MonitorPlugin()
        self.plugin.configure({"data_dir": str(data_dir), "on_wake": self.on_wake})
        self._engine: AgentEngine | None = None
        self._wake_in_flight = False

    def build_engine(self) -> AgentEngine:
        self.plugin.initialize()
        engine = AgentEngine(
            chat=self.chat,
            plugins=[self.plugin],
            max_iterations=self.config.max_iterations,
        )
        # The register_monitor tool injects this provider via
        # @tool(inject=["session_id_provider"]) and stamps every monitor
        # with the current session, so wakes resume the right tree.
        engine.context.extra["session_id_provider"] = lambda: self.tree.session_id
        self._engine = engine
        return engine

    # ── Wake channel ────────────────────────────────────────────────────

    def on_wake(self, event) -> None:
        """Called by the plugin on every fire (queue is the source of truth)."""
        print(f"\n[WAKE] monitor {event.monitor!r} fired ({event.kind})")
        # Re-entrancy guard: a fire while a run is in flight is left in
        # the queue; it is drained into the *next* run below.
        if self._wake_in_flight:
            print("[WAKE] run in flight — wake stays queued for the next turn")
            return
        # Schedule on the running loop; on_wake itself is sync by contract.
        asyncio.get_running_loop().create_task(self._run_wake_turn())

    async def _run_wake_turn(self) -> None:
        self._wake_in_flight = True
        try:
            drained = self.plugin.drain_pending_wakes(self.tree.session_id)
            if not drained:
                return
            message = render_wake_message(drained)
            await self.run_turn(message, source="monitor")
        finally:
            self._wake_in_flight = False

    # ── Runs ────────────────────────────────────────────────────────────

    async def run_turn(self, text: str, *, source: str = "user") -> None:
        assert self._engine is not None
        self.tree.append(self.tree.get_leaves()[-1], Message(role="user", content=text))
        await self.storage.save(self.tree)

        path = self.tree.get_path(self.tree.get_leaves()[-1])
        print(f"\n{'─' * 60}\n[{source}] {text[:200]}{'…' if len(text) > 200 else ''}")
        result = await self._engine.run(path, ModelConfig(model=self.config.model))
        self.tree.append(
            self.tree.get_leaves()[-1],
            Message(role="assistant", content=result.final_content),
        )
        await self.storage.save(self.tree)
        print(f"[agent] {result.final_content}")


async def main() -> None:
    from phoson_llm.factory import build_chat

    config = load_config()
    base = Path("~/.phoson").expanduser()
    host = WakeHost(
        chat=build_chat(config),
        config=config,
        data_dir=base / "monitors",
        sessions_dir=base / "sessions",
    )
    host.build_engine()
    await host.plugin.ensure_started()  # resurrect monitors from a previous run

    # Ask the agent to set up a monitor (it has the register_monitor tool).
    await host.run_turn(
        "Please register a monitor named 'heartbeat' of kind 'interval' "
        'with spec {"seconds": 10, "once": true} so I get woken up in '
        "10 seconds. Just call the tool and confirm.",
    )

    print("\nHost idle — waiting for the monitor to fire (Ctrl+C to stop)…")
    try:
        await asyncio.Event().wait()  # idle forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await host.plugin.aclose()
        print("\nShutdown complete. Monitors stay registered on disk.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
