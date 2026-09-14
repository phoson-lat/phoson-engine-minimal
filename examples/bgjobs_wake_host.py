"""Standalone host example for the background-jobs plugin (#217).

Demonstrates the full job → wake loop **without the CLI**: the host keeps one
`ConversationTree` (persisted via `JsonlStorage`), the agent starts a detached
background job during a run, and when the job finishes the host's `on_wake`
callback starts a *new* run that **resumes the same conversation tree** with
the result (exit code + output tail + duration) as new input.

Run (needs a configured provider in ~/.phoson/config.toml):

    python examples/bgjobs_wake_host.py

Press Ctrl+C twice to exit. Watch `~/.phoson/bgjobs/` while it runs:
`jobs.json` holds the registry, `wakes.jsonl` the completion queue and
`jobs/<id>/output.log` the captured output.

The Phoson CLI ships the same integration out of the box (opt-in with
`enable_bgjobs = true`); this example exists to show how a custom embedded
host (e.g. Phoson-Core) wires the wake channel itself.
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
from phoson_plugin_bgjobs import BgJobsPlugin, render_wake_message
from phoson_agent.sessions import JsonlStorage, ConversationTree


class JobWakeHost:
    """A minimal embedded host: one engine, one tree, one wake channel."""

    def __init__(self, chat, config, data_dir: Path, sessions_dir: Path) -> None:
        self.chat = chat
        self.config = config
        self.sessions_dir = sessions_dir
        self.storage = JsonlStorage(base_path=sessions_dir)
        self.tree = ConversationTree.new(session_id="demo-bgjobs-session")
        self.plugin = BgJobsPlugin()
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
        # The run_bg_job tool injects this provider via
        # @tool(inject=["session_id_provider"]) and stamps every job with the
        # current session, so wakes resume the right tree.
        engine.context.extra["session_id_provider"] = lambda: self.tree.session_id
        self._engine = engine
        return engine

    # ── Wake channel ────────────────────────────────────────────────────

    def on_wake(self, event) -> None:
        """Called by the plugin on every completion (queue is the source of truth)."""
        print(
            f"\n[WAKE] job {event.name!r} finished (state={event.payload.get('state')})"
        )
        # Re-entrancy guard: a fire while a run is in flight stays queued and
        # is drained into the *next* run below.
        if self._wake_in_flight:
            print("[WAKE] run in flight — wake stays queued for the next turn")
            return
        asyncio.get_running_loop().create_task(self._run_wake_turn())

    async def _run_wake_turn(self) -> None:
        self._wake_in_flight = True
        try:
            drained = self.plugin.drain_pending_wakes(self.tree.session_id)
            if not drained:
                return
            message = render_wake_message(drained)
            await self.run_turn(message, source="wake")
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
    host = JobWakeHost(
        chat=build_chat(config),
        config=config,
        data_dir=base / "bgjobs",
        sessions_dir=base / "sessions",
    )
    host.build_engine()
    await host.plugin.ensure_started()  # reconcile jobs from a previous run

    # Ask the agent to start a detached job (it has the run_bg_job tool).
    await host.run_turn(
        "Use run_bg_job to start a background job named 'demo' that runs "
        "`sleep 5 && echo done`. Do not wait for it — just start it and "
        "confirm you will act when it finishes."
    )

    print("\nHost idle — waiting for the job to finish (Ctrl+C to stop)…")
    try:
        await asyncio.Event().wait()  # idle forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await host.plugin.aclose()
        print("\nShutdown complete. Jobs stay registered on disk.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
