"""Monitor plugin: long-running watchers that can re-activate the agent.

Exposes ``register_monitor``/``list_monitors``/``stop_monitor`` tools so the
agent can watch for things that outlive the current run (a file appearing, a
command failing, a URL changing, a timer) and be woken up when they fire.

Design (IMPROVEMENTS.md I-126, docs/plans/I-126.md):

- **Disk is the source of truth.** The registry (``monitors.json``) and the
  wake queue (``wake.jsonl``) live under ``data_dir`` and survive process
  restarts. In-memory asyncio tasks are a cache: they die when the host
  rebuilds its engine or exits, and ``ensure_started()`` resurrects every
  ``running`` monitor from disk.
- **Wakes go to a persistent queue first.** A host with a live event loop
  may additionally pass an ``on_wake`` callable (called fire-and-forget on
  every fire); the CLI drains the queue into the next user turn instead.
- **No new engine contract.** The plugin only uses the canonical
  ``Plugin`` lifecycle plus ``@tool``; hosts integrate via duck-typed
  methods (``ensure_started``/``drain_pending_wakes``).

Security note (``command`` kind): registering a monitor is a tool call the
model makes, so it passes the host's permission gate like any other tool
call with the exact command visible. The command is then re-run verbatim on
every interval tick — hosts should treat a registered monitor as approved
shell access until ``stop_monitor``.
"""

import re
import json
import asyncio
import logging
from typing import Any, Literal, cast
from collections.abc import Callable, Awaitable

from phoson_agent import (
    Plugin,
    AgentTool,
    KeyValueBlock,
    CliCommandSpec,
    CliCommandContext,
    CliCommandInvocation,
    tool,
)

from .kinds import run_kind, validate_spec
from .storage import WakeEvent, WakeQueue, MonitorDef, MonitorStore

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = "~/.phoson/monitors"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def render_wake_message(events: list[WakeEvent]) -> str:
    """Render consumed wake events as a user-message header (pure).

    The host prepends this to the user's input so the model sees the
    monitor findings in context.
    """
    if not events:
        return ""
    lines = [
        "[MONITOR EVENTS] A background monitor fired while you were idle. "
        "Review the findings below and act on them."
    ]
    for event in events:
        lines.append("")
        lines.append(f"[{event.monitor}] kind={event.kind} fired_at={event.fired_at}")
        if event.payload:
            for key, value in event.payload.items():
                if isinstance(value, str):
                    lines.append(f"  {key}: {value}")
                else:
                    lines.append(f"  {key}: {json.dumps(value)}")
    return "\n".join(lines)


class MonitorPlugin(Plugin):
    """Long-running monitors that persist across runs and re-activate the agent.

    Configuration (via ``configure``):
        data_dir: State directory (default ``~/.phoson/monitors``).
        on_wake: Optional callable (async or sync) invoked as
            ``on_wake(WakeEvent)`` whenever a monitor fires, in addition to
            the persistent queue. Live hosts (e.g. an embedded runtime) use
            it to start a new run immediately; the CLI does not.
        max_pending_wakes: Pending events kept per monitor before the
            oldest is dropped (anti-storm, default 5).
        default_session_id: Session id stamped on wakes when the host did
            not inject a ``session_id_provider`` into the agent context.
    """

    def __init__(self) -> None:
        self._data_dir: str = _DEFAULT_DATA_DIR
        self._on_wake: Callable[[WakeEvent], Awaitable[None] | None] | None = None
        self._max_pending_wakes = 5
        self._default_session_id = ""

        self._store: MonitorStore | None = None
        self._queue: WakeQueue | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake_lock = asyncio.Lock()

    # ── Plugin contract ───────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "phoson-plugin-monitor"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Long-running monitors that wake the agent when their condition fires"

    def configure(self, config: dict[str, Any]) -> None:
        """Merge config; keys absent from ``config`` keep their current value.

        The loader calls ``configure`` again (with an empty dict) when the
        same instance is reloaded after an engine rebuild, so this must
        never reset state.
        """
        if "data_dir" in config:
            self._data_dir = str(config["data_dir"])
        if "on_wake" in config:
            on_wake = config["on_wake"]
            if on_wake is not None and not callable(on_wake):
                raise ValueError("on_wake must be a callable (or null to disable)")
            # `callable()` narrows Any to a generic Callable; re-assert the
            # contracted type (runtime check above is the real guard).
            self._on_wake = cast(
                Callable[[WakeEvent], Awaitable[None] | None] | None, on_wake
            )
        if "max_pending_wakes" in config:
            self._max_pending_wakes = int(config["max_pending_wakes"])
        if "default_session_id" in config:
            self._default_session_id = str(config["default_session_id"] or "")

    def initialize(self) -> None:
        """Load (or create) the on-disk registry and wake queue.

        Sync on purpose: it may run before any event loop exists (engine
        construction). Idempotent: a second call (e.g. from a tool handler
        on a fresh host) is a no-op once the store is loaded. Tasks are
        started lazily by ``ensure_started()``.
        """
        if self._store is not None and self._queue is not None:
            return
        self._store = MonitorStore(self._data_dir)
        self._queue = WakeQueue(self._data_dir, self._max_pending_wakes)
        logger.debug(
            "Monitor plugin initialized: %d monitor(s), %d pending wake(s) in %s",
            len(self._store.list()),
            self._queue.pending_count(),
            self._data_dir,
        )

    async def aclose(self) -> None:
        """Cancel all monitor tasks without persisting a state change.

        Monitors stay ``running`` on disk: the next host (CLI restart or
        engine rebuild) resurrects them via ``ensure_started()``.
        """
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._store is not None:
            self._store.flush()

    def cleanup(self) -> None:
        """Sync fallback: best-effort cancel when no loop can await us.

        The formal shutdown is ``aclose()``; hosts that only call
        ``cleanup()`` (legacy contract) still lose their tasks when the
        process exits, which is exactly the crash-recovery path.
        """
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()

    # ── Host-facing duck-typed hooks (documented, not engine contract) ────

    async def ensure_started(self) -> None:
        """(Re)start a task for every ``running`` monitor lacking one.

        Idempotent; safe to call from the host on engine rebuild or from a
        tool handler (first call lazily, once a loop exists).
        """
        if self._store is None or self._queue is None:
            self.initialize()
        assert self._store is not None and self._queue is not None
        for monitor in self._store.list():
            task = self._tasks.get(monitor.name)
            if not monitor.is_running:
                continue
            if task is not None and not task.done():
                continue
            if task is not None:
                self._tasks.pop(monitor.name, None)
            self._spawn_task(monitor)

    def drain_pending_wakes(self, session_id: str | None) -> list[WakeEvent]:
        """Consume and return pending wakes for ``session_id`` (None = all).

        Called by the CLI before a run to fold monitor findings into the
        next user message. The queue is the source of truth, so this is
        safe to call from any thread the host has (I/O is cheap JSON).
        """
        if self._queue is None:
            self.initialize()
        assert self._queue is not None
        return self._queue.consume([e.id for e in self._queue.pending(session_id)])

    def pending_wakes(self, session_id: str | None) -> list[WakeEvent]:
        """Non-destructive view of pending wakes (None = all sessions).

        Used by the host's autonomous wake loop to *peek* before deciding
        to re-activate the agent, so a skipped tick never consumes a fire.
        """
        if self._queue is None:
            self.initialize()
        assert self._queue is not None
        return self._queue.pending(session_id)

    def monitor_status(self) -> str | None:
        """Short status string for active monitors, or ``None`` when none.

        Optional duck-typed host hook (not part of the ``Plugin``
        contract): a UI host calls it to surface "monitors are running" in
        a header or prompt. It is in-memory only (no disk I/O), safe to
        call on every paint, and UI-agnostic — it returns plain text, the
        host decides where and how to render it (or to ignore it).
        """
        if self._store is None:
            self.initialize()
        assert self._store is not None
        running = [m for m in self._store.list() if m.is_running]
        if not running:
            return None
        shown = ", ".join(m.name for m in running[:4])
        extra = len(running) - min(len(running), 4)
        if extra > 0:
            shown = f"{shown} +{extra}"
        return f"⏳ {shown}"

    # ── CLI extension: /monitors ──────────────────────────────────────────

    def get_commands(self) -> list[CliCommandSpec]:
        return [
            CliCommandSpec(
                names=("/monitors",),
                help="List background monitors, their state and pending wakes",
                handler="handle_monitors",
                category="Plugins",
            )
        ]

    async def handle_monitors(
        self, command: CliCommandInvocation, context: CliCommandContext
    ) -> bool:
        self.initialize()
        assert self._store is not None and self._queue is not None
        args = command.args.strip()

        if args.lower() in ("pending", "wakes"):
            events = self._queue.pending(context.session_id or None)
            context.notify(
                "info",
                f"{len(events)} pending monitor wake(s) for this session."
                if events
                else "No pending monitor wakes for this session.",
            )
            return True

        if args:
            target = self._store.get(args)
            monitors = [target] if target is not None else []
            if not monitors:
                context.notify("warn", f"No monitor named {args!r}.")
                return True
        else:
            monitors = self._store.list()

        items: list[tuple[str, str]] = []
        for monitor in monitors:
            status = "running" if monitor.is_running else "stopped"
            last = monitor.last_fired_at or "never"
            items.append((monitor.name, f"{status} · {monitor.kind}"))
            items.append(("", f"last fired: {last} · fires: {monitor.fire_count}"))
        pending = self._queue.pending(context.session_id or None)
        items.append(("", f"pending wakes (this session): {len(pending)}"))

        try:
            context.ui.publish(
                KeyValueBlock(
                    id="monitor-plugin:status",
                    title="Monitors",
                    items=tuple(items),
                )
            )
        except Exception:  # noqa: BLE001 — non-interactive hosts
            logger.debug("plugin_ui unavailable for /monitors", exc_info=True)
        context.notify("info", f"{len(monitors)} monitor(s) listed.")
        return True

    # ── Tools ─────────────────────────────────────────────────────────────

    def get_tools(self) -> list[AgentTool]:
        plugin = self

        @tool(inject=["session_id_provider"])
        async def register_monitor(
            name: str,
            kind: Literal["interval", "file", "command"],
            spec: dict,
            *,
            session_id_provider: Callable[[], str] | None = None,
        ) -> str:
            """Register a background monitor that wakes this agent when it fires.

            Kinds and their spec fields:
            - "interval": {"seconds": number >= 1, "once": bool (default false)}.
              Fires after N seconds, or every N seconds when once=false.
            - "file": {"path": string, "event": "created"|"changed"|"both",
              "poll_seconds": number >= 0.2 (default 2)}. Polls a path or
              glob (e.g. "build/*.bin"); "changed" means mtime or size
              changed, "created" means new matching files.
            - "command": {"command": string, "interval_seconds": number
              (default 60), "timeout_seconds": number > 0 (default 60)}.
              Runs the shell command on an interval; fires on non-zero exit,
              timeout, or changed output. The command runs unattended —
              only register what is safe to re-run.

            The monitor keeps running after this run ends (it is persisted
            and resurrected on restart). When it fires, its findings are
            delivered to this session as new input. Use stop_monitor(name)
            to remove it.
            """
            return await plugin._register_monitor(name, kind, spec, session_id_provider)

        @tool
        async def list_monitors() -> str:
            """List registered background monitors with state and last fire."""
            return await plugin._list_monitors()

        @tool
        async def stop_monitor(name: str) -> str:
            """Stop a background monitor by name and remove it from the registry.

            Pending wake events already fired are kept (the host delivers
            them on the next turn).
            """
            return await plugin._stop_monitor(name)

        return [register_monitor, list_monitors, stop_monitor]

    # ── Tool implementations ──────────────────────────────────────────────

    async def _register_monitor(
        self,
        name: str,
        kind: str,
        spec: dict,
        session_id_provider: Callable[[], str] | None,
    ) -> str:
        self.initialize()
        assert self._store is not None and self._queue is not None

        if not _NAME_RE.match(name or ""):
            return (
                f"Error: invalid monitor name {name!r}. Use 1-64 chars from "
                "[A-Za-z0-9._-], starting with a letter or digit."
            )
        if self._store.get(name) is not None:
            return f"Error: monitor {name!r} already exists."

        try:
            normalized = validate_spec(kind, spec)
        except ValueError as exc:
            return f"Error: {exc}"

        session_id = ""
        if callable(session_id_provider):
            try:
                session_id = str(session_id_provider() or "")
            except Exception:  # noqa: BLE001 — a broken provider is not fatal
                logger.warning("session_id_provider failed", exc_info=True)
        if not session_id:
            session_id = self._default_session_id

        monitor = MonitorDef(
            name=name, kind=kind, spec=normalized, session_id=session_id
        )
        self._store.add(monitor)
        # Spawn directly (do NOT also call ensure_started(): it would see
        # the monitor we just added and spawn a second task).
        self._spawn_task(monitor)
        return (
            f"Monitor {name!r} registered (kind={kind}, session="
            f"{session_id or 'unbound'}). It fires findings into this "
            f"session when its condition is met; stop it with "
            f"stop_monitor({name!r})."
        )

    async def _list_monitors(self) -> str:
        self.initialize()
        assert self._store is not None and self._queue is not None
        monitors = self._store.list()
        if not monitors:
            return "No monitors registered."
        lines = ["Monitors:"]
        for monitor in monitors:
            last = monitor.last_fired_at or "never"
            pending = len(self._queue.pending(monitor.session_id))
            lines.append(
                f"- {monitor.name} [{monitor.kind}] {monitor.state} · "
                f"spec={monitor.spec} · last fired: {last} · "
                f"fires: {monitor.fire_count} · pending wakes: {pending}"
            )
        return "\n".join(lines)

    async def _stop_monitor(self, name: str) -> str:
        self.initialize()
        assert self._store is not None

        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        monitor = self._store.get(name)
        if monitor is None:
            return f"Error: monitor {name!r} does not exist."
        self._store.remove(name)
        return f"Monitor {name!r} stopped and removed from the registry."

    # ── Task management ───────────────────────────────────────────────────

    def _spawn_task(self, monitor: MonitorDef) -> None:
        self._tasks[monitor.name] = asyncio.create_task(
            self._run_monitor(monitor),
            name=f"monitor:{monitor.name}",
        )

    async def _run_monitor(self, monitor: MonitorDef) -> None:
        try:
            await run_kind(monitor, self._fire)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken kind must not kill the host
            logger.exception("Monitor %r crashed", monitor.name)
            self._mark_stopped(monitor.name, reason="crash")
            return
        # Natural completion (e.g. once=true interval): mark stopped.
        self._mark_stopped(monitor.name, reason="completed")

    def _mark_stopped(self, name: str, *, reason: str) -> None:
        self._tasks.pop(name, None)
        if self._store is None:
            return
        monitor = self._store.get(name)
        if monitor is None or not monitor.is_running:
            return
        stopped = MonitorDef(
            name=monitor.name,
            kind=monitor.kind,
            spec=monitor.spec,
            state="stopped",
            session_id=monitor.session_id,
            created_at=monitor.created_at,
            last_fired_at=monitor.last_fired_at,
            last_check_at=monitor.last_check_at,
            fire_count=monitor.fire_count,
        )
        self._store.replace(stopped)
        logger.info("Monitor %r stopped (%s)", name, reason)

    # ── Fire path ─────────────────────────────────────────────────────────

    async def _fire(self, name: str, kind: str, payload: dict[str, Any]) -> None:
        assert self._store is not None and self._queue is not None
        async with self._wake_lock:
            monitor = self._store.get(name)
            if monitor is None:
                return  # stopped concurrently; drop the fire
            event = WakeEvent.create(
                monitor=name,
                kind=kind,
                session_id=monitor.session_id,
                payload=payload,
            )
            stored = self._queue.append(event)
            self._store.mark_fired(name)
            if stored is None:
                return  # deduped: an identical wake is already pending
            event = stored

        if self._on_wake is not None:
            try:
                asyncio.get_running_loop().create_task(self._invoke_on_wake(event))
            except RuntimeError:  # pragma: no cover — fire always has a loop
                logger.debug("No running loop to dispatch on_wake")

    async def _invoke_on_wake(self, event: WakeEvent) -> None:
        on_wake = self._on_wake
        if on_wake is None:
            return
        try:
            result = on_wake(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — a broken host callback never
            # loses the queued event; the queue remains the source of truth.
            logger.exception("on_wake callback failed for wake %s", event.id)


def create_plugin() -> MonitorPlugin:
    """Factory for the path-based loader (``path:./phoson_plugin_monitor/
    _plugin.py``).
    """
    return MonitorPlugin()


__all__ = [
    "MonitorPlugin",
    "create_plugin",
    "render_wake_message",
]
