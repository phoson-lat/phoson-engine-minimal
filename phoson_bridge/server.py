"""JSON-RPC server over NDJSON (stdio) that hosts the engine sessions.

Transport
---------
- One line = one JSON object (NDJSON), in both directions.
- Frontend requests carry an ``id``; responses reuse that ``id``.
- Notifications (streaming) carry no ``id`` and use ``method``.

Multi-session (decision #5)
---------------------------
The ``SessionController``/``AgentEngine`` is *single-flight*: it does not allow
two concurrent runs on the same instance. To allow N simultaneous sessions,
``SessionManager`` keeps **one ``PhosonRepl`` instance per session** (each with
its own ``SessionController``, ``GuiSink`` and ``GuiConfirmation``), all on the
SAME asyncio loop of the sidecar.

Reuse of ``PhosonRepl`` (decision #3)
-------------------------------------
The prompt_toolkit part is not used (that lives in ``PhosonRepl.run()``).
``PhosonRepl`` is reused as a headless *session engine*: it exposes the
passthroughs (``set_model``, ``load_session``, ``compact_context``,
``undo_last_turn``, ``jump_to_user_turn``, ``shutdown``…) that the CLI
``/commands`` already expect, so that a future ``GuiCommandHost`` +
``CommandHandler`` works without touching the core.
"""

import os
import sys
import copy
import json
import time
import uuid
import asyncio
import logging
import threading
from typing import Any, TextIO, cast
from pathlib import Path
from collections.abc import Callable, Sequence

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import PhosonConfig, load_config, has_configured_provider
from phoson_cli.session_utils import (
    find_wake_plugins,
    engine_masked_count,
    engine_visible_tools,
)

from . import extras as _extras
from . import mcp_catalog
from .stt import (
    SttManager,
    merge_plugin_config,
)
from .stt import (
    report as stt_report,
)
from .stt import (
    transcribe_wav as stt_transcribe_wav,
)
from .sink import GuiSink
from .protocol import dump, to_jsonable
from .plugin_ui import install as install_plugin_ui
from .mcp_config import clean_spec, merge_secret_map
from .confirmation import GuiConfirmation

# Extras installed from Settings (`~/.phoson/extras/<extra>`) go into
# `sys.path` BEFORE the engine builds its plugins: otherwise, a plugin
# installed yesterday would be skipped today until the next restart.
_extras.bootstrap_paths()

log = logging.getLogger("phoson_bridge")

#: `session.list` reads and parses every `.jsonl` (~3 s with many histories),
#: so it is cached; the TTL covers changes made by ANOTHER sidecar (they share
#: `sessions_dir`) and local mutations invalidate it immediately.
_SESSION_LIST_TTL = 4.0
#: `stt.status` probes the voice plugin/engine (~150 ms); it rarely changes.
_STT_STATUS_TTL = 5.0
#: Methods that can alter the list of saved sessions.
_SESSION_MUTATING = frozenset(
    {
        "turn.run",
        "session.delete",
        "session.new",
        "session.undo",
        "session.rewind",
        "session.compact",
    }
)
#: The model catalog rarely changes and each query is a network round-trip
#: (~430 ms); the picker opens it instantly and a turn must not pay for it.
_MODELS_LIST_TTL = 300.0


def _provider_of(cfg: Any) -> str:
    """Normalized (lowercase) provider of an engine config."""
    return str(getattr(cfg, "provider", "") or "").strip().lower()


def _config_for_provider(cfg: PhosonConfig, provider: str) -> PhosonConfig:
    """Clone of *cfg* with `provider` (and its default model) applied.

    `models.list` accepts an explicit `provider` so that Settings can query the
    catalog of the provider the user just picked in the form — not yet passed
    through `config.set` — without touching the engine's live config.
    The clone's model is the provider's `default_model`; that way the listing
    (and its fallback if the network fails) does not carry over another
    provider's id.
    """
    variant = copy.copy(cfg)
    variant.provider = provider
    try:
        from phoson_cli.models import load_models_file, provider_settings

        settings = provider_settings(load_models_file(), provider)
        variant.model = settings.get("default_model") or cfg.model
    except Exception:  # noqa: BLE001 — no defaults catalog, keep the model
        variant.model = cfg.model
    return variant


#: Token coalescing window (seconds). The engine emits one `agent.event`
#: PER TOKEN (hundreds per second), and each notification is a serialized
#: JSON + IPC to the WebView + dispatch in Tauri. Accumulating 30 ms and
#: flushing them together in a single notification is imperceptible to the
#: user and collapses the stream to ~33 lines/second. Only applies to additive
#: content events (text/reasoning); the rest keep their order.
_TOKEN_BATCH_SECONDS = 0.03
_TOKEN_EVENT_TYPES = frozenset({"AgentTokenEvent", "AgentReasoningEvent"})

#: Watchdog thresholds (seconds). Two distinct situations:
#:  * silent start: the handler never writes **anything** — the engine is
#:    frozen (the Windows bug: a `git` call blocking the event loop);
#:  * prolonged silence: it wrote and then went quiet (e.g. a call to the
#:    provider hung without a timeout).
#: A slow model triggers NOTHING: it writes `session.user_message`/
#: `AgentStartEvent` in the first ms and only stays quiet until tokens flow.
#: They can be forced via env to test the behavior by hand:
#:   PHOSON_WATCHDOG_SECONDS=3 PHOSON_WATCHDOG_SILENCE_SECONDS=5
_WATCHDOG_SECONDS: dict[str, float] = {
    "turn.run": 15.0,
    "plugins.install": 180.0,
    # Installing an extra downloads wheels (onnxruntime, Pillow…): it can take
    # several minutes without the sidecar showing any sign of life.
    "plugins.installExtra": 900.0,
    # OAuth login waits for the user (device code or browser): it is normal
    # for nothing to come out for minutes, so it is not a hang.
    "auth.login": 15 * 60.0,
}
_WATCHDOG_SILENCE_SECONDS: dict[str, float] = {"turn.run": 60.0}
_WATCHDOG_DEFAULT_SECONDS = 30.0
_WATCHDOG_DEFAULT_SILENCE_SECONDS = 120.0
#: Max warnings per in-flight RPC (the watchdog reschedules itself while the
#: problem persists, but not forever).
_WATCHDOG_MAX_WARNINGS = 3

#: Providers that authenticate with a **subscription** (OAuth) rather than an
#: API key. The token is stored by `phoson_llm.auth` in `~/.phoson/auth*.json`;
#: `config.toml` only remembers which provider is active.
_AUTH_PROVIDERS: dict[str, str] = {
    "chatgpt": "phoson_llm.auth.chatgpt",
    "google-code-assist": "phoson_llm.auth.code_assist",
}


def _config_path() -> Path:
    """Path of the config shared with the CLI (the same one `load_config` reads)."""
    return Path("~/.phoson/config.toml").expanduser()


def _peer_identity(params: dict[str, Any]) -> tuple[str, str]:
    """Normalized name/team for a Teams session (empty = regular session)."""
    from phoson_plugin_peers.storage import normalize_name

    raw_name = str(params.get("peerName") or "").strip()
    name = normalize_name(raw_name) if raw_name else ""
    team = normalize_name(str(params.get("peerTeam") or "default"), "team name")
    return name, team


def _blank_metrics(session_key: str, cfg: PhosonConfig) -> dict[str, Any]:
    """Zeroed metrics for a session whose engine has not been built yet.

    `initialize` returns them when there is no usable provider (onboarding): the
    frontend stores this block in the session view and the engine will be built
    later (first `turn.run`), with config.toml already written.
    """
    return {
        "sessionId": session_key,
        "costUsd": 0.0,
        "credits": 0,
        "tokens": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "steps": 0,
        "contextTokens": 0,
        # Same default as the controller before resolving the real window.
        "contextWindow": 128_000,
        "model": cfg.model,
        "provider": cfg.provider,
        "isRunning": False,
    }


def _watchdog_delays(method: str) -> tuple[float, float]:
    """(silent-start threshold, silence threshold) for a method."""
    start = float(
        os.environ.get("PHOSON_WATCHDOG_SECONDS")
        or _WATCHDOG_SECONDS.get(method, _WATCHDOG_DEFAULT_SECONDS)
    )
    silence = float(
        os.environ.get("PHOSON_WATCHDOG_SILENCE_SECONDS")
        or _WATCHDOG_SILENCE_SECONDS.get(method, _WATCHDOG_DEFAULT_SILENCE_SECONDS)
    )
    return start, silence


class SessionManager:
    """One ``PhosonRepl`` per session, sharing the sidecar loop."""

    def __init__(self, base_config: PhosonConfig, emit) -> None:
        self._base_config = base_config
        self._emit = emit
        self._loop = asyncio.get_running_loop()
        self._repls: dict[str, PhosonRepl] = {}
        # Sessions created but not yet built (lazy): `create` is cheap and
        # `get` pays for the `PhosonRepl` the first time someone needs it.
        self._pending: dict[str, tuple[Any, Any, Any]] = {}
        # In-progress builds, shared among the RPCs awaiting them.
        self._building: dict[str, asyncio.Task[PhosonRepl]] = {}
        self._confirmations: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Set by the `Bridge`: called (synchronously) when an autonomous turn
        # finishes, to refresh the session metrics.
        self.on_wake_turn_done: Callable[[str], None] | None = None

    # -- lifecycle ------------------------------------------------------------
    def create(self, peer_name: str = "", peer_team: str = "default") -> str:
        """Reserve a session. Does NOT build the ``PhosonRepl`` (≈1.7 s).

        Building it here blocked the event loop before the sidecar started reading
        stdin: the first request of each workspace paid for the full engine startup
        before receiving a single byte. With lazy building, cheap RPCs
        (`session.list`, `stt.status`, `fs.*`) respond immediately and `turn.run`
        emits `session.turn.started` before building anything.
        """
        key = uuid.uuid4().hex
        # Per-session config: the controller mutates config.model/provider.
        # `copy.copy` (shallow) is enough: only scalars are mutated; the config
        # carries a `mappingproxy` in `_secret_sources` that breaks deepcopy.
        config = copy.copy(self._base_config)
        # Identity PER SESSION, not global in config.toml: one sidecar can host
        # several agents and other workspaces share the team bus.
        config.peer_name = peer_name
        config.peer_team = peer_team
        sink = GuiSink(key, self._emit)
        confirmation = GuiConfirmation(key, self.request_user)
        # The sink can read the live title from the tree (`on_session_title` hook).
        sink.title_provider = lambda k=key: (  # pyright: ignore[reportAttributeAccessIssue]
            self._repls[k].tree.title if k in self._repls else ""
        )
        self._pending[key] = (config, sink, confirmation)
        return key

    def _build(self, key: str, parts: tuple[Any, Any, Any]) -> PhosonRepl:
        """Build a session's ``PhosonRepl`` (seconds of CPU/IO)."""
        config, sink, confirmation = parts
        _t = time.perf_counter()
        repl = PhosonRepl(config, sink=sink, confirmation=confirmation)
        log.info("[perf] bridge.init %.0fms", (time.perf_counter() - _t) * 1000)
        return repl

    async def get(self, key: str) -> PhosonRepl:
        """Return the session's ``PhosonRepl``, building it if missing.

        Building costs **seconds**, so it runs in a thread (`asyncio.to_thread`):
        running it on the event loop froze everything else — in a real trace, four
        `session.list` calls and the stream of other sessions waited 7.7 s behind a
        single `bridge.init`. Concurrent RPCs share the in-progress build instead
        of duplicating it.
        """
        repl = await self._get(key)
        # `_rebuild_engine` (model/config change) injects the engine's
        # `SinkPluginUiService`, which renders UiBlocks to Rich: we put ours
        # back (idempotent and cheap) before any use.
        install_plugin_ui(repl)
        return repl

    async def _get(self, key: str) -> PhosonRepl:
        repl = self._repls.get(key)
        if repl is not None:
            return repl
        building = self._building.get(key)
        if building is not None:
            return await building

        parts = self._pending.pop(key, None)
        if parts is None:
            raise KeyError(f"sesión desconocida: {key}")

        async def _run() -> PhosonRepl:
            try:
                built = await asyncio.to_thread(self._build, key, parts)
                self._repls[key] = built
                # Already on the loop: the wakes loop cannot be created in the thread.
                self._wire_wakes(key, built)
                return built
            except Exception:
                # The session is NOT lost on a failed build (typical: a key
                # pasted wrong during onboarding): it returns to `_pending` and
                # the next RPC retries the build with the corrected config.
                self._pending[key] = parts
                raise
            finally:
                self._building.pop(key, None)

        task = asyncio.create_task(_run())
        self._building[key] = task
        return await task

    # -- autonomous wake-up (monitors, background jobs, peers) ----------------
    def _wire_wakes(self, key: str, repl: PhosonRepl) -> None:
        """Start the loop that wakes the agent up when a monitor fires.

        The CLI starts it from `PhosonRepl.run()`, which the bridge does not use:
        without this, the monitor fires, the wake stays queued on disk and the agent
        only finds out with the user's next message. Each autonomous turn goes
        through `_run_wake_turn` so that the GUI closes it just like a `turn.run`.
        """
        controller = repl._controller
        set_starter = getattr(controller, "set_operation_starter", None)
        if callable(set_starter):
            set_starter(
                lambda coro, _kind, k=key: asyncio.create_task(
                    self._run_wake_turn(k, coro), name=f"wake-turn:{k}"
                )
            )
        start = getattr(controller, "start_monitor_wake_loop", None)
        if callable(start):
            start()
        # Engines <= 0.57 only start the loop with an `enable_*` flag on: with
        # the plugin loaded from `[defaults].plugins` (flag off) the monitor
        # fires and nobody wakes the agent. If wake providers are loaded and
        # the loop did not start, it is started here.
        loop_body = getattr(controller, "_monitor_wake_loop", None)
        if (
            callable(loop_body)
            and getattr(controller, "_monitor_wake_task", "absent") is None
            and find_wake_plugins(list(getattr(repl.engine, "_loaded_plugins", [])))
        ):
            controller._monitor_wake_task = asyncio.create_task(
                cast(Any, loop_body()), name="monitors:wake-loop"
            )

    async def _run_wake_turn(self, key: str, coro: Any) -> None:
        """Run an autonomous turn and emit its `session.assistant.done`.

        The GUI opens the turn when it receives `session.user_message` without a send
        of its own in progress; it is closed here. If a user turn consumed the wakes
        first (nothing ran), nothing is emitted.
        """
        sink = self._repls[key]._controller.sink
        opened = getattr(sink, "user_messages", 0)
        outcome: Any = None
        status = "error"
        try:
            outcome = await coro
            status = getattr(outcome, "status", None) or "done"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            # Older engines do not return the wake outcome: the sink's counter
            # tells whether the turn got opened.
            if outcome is not None or getattr(sink, "user_messages", 0) > opened:
                self._emit(
                    "session.assistant.done",
                    {
                        "sessionId": key,
                        "status": status,
                        "errorCode": getattr(outcome, "error_code", None),
                        "finalContent": getattr(outcome, "final_content", None),
                        "source": "wake",
                    },
                )
                if self.on_wake_turn_done is not None:
                    self.on_wake_turn_done(key)

    async def close(self, key: str) -> None:
        self._pending.pop(key, None)
        building = self._building.get(key)
        if building is not None:
            try:
                await building  # don't leave an engine building in flight
            except Exception:  # noqa: BLE001 — closing takes precedence
                pass
        repl = self._repls.pop(key, None)
        if repl is not None:
            await repl.shutdown()

    def ensure_any(self) -> str:
        """Ensure at least one session exists and return a key.

        The front-end can close the default session; without this, the bridge ends
        up with no sessions and any RPC that uses them (initialize, fs.setCwd…)
        fails with "unknown session".
        """
        # `get` takes the session out of `_pending` while it builds: without
        # checking `_building` here a second session would be created in parallel.
        if self._building:
            return next(iter(self._building))
        if self._pending:
            return next(iter(self._pending))
        if self._repls:
            return next(iter(self._repls))
        return self.create()

    async def close_all(self) -> None:
        for key in list(self._building) + list(self._pending) + list(self._repls):
            await self.close(key)

    def keys(self) -> list[str]:
        return list(self._building) + list(self._pending) + list(self._repls)

    def storage(self):
        # All sessions share sessions_dir; any controller will do.
        if self._repls:
            return next(iter(self._repls.values())).storage
        from phoson_agent.sessions import JsonlStorage

        return JsonlStorage(base_path=self._base_config.sessions_dir)

    # -- access without building the engine ----------------------------------
    # On a machine without `~/.phoson/config.toml` or keys in the environment the
    # engine CANNOT be built (`build_chat` requires the active provider's
    # credential), so the configuration plane (onboarding, settings, model
    # catalog) works against the session's reserved config without going
    # through `get()`.

    def is_built(self, key: str) -> bool:
        """Has the session's engine (``PhosonRepl``) already been built?"""
        return key in self._repls

    def repl_if_built(self, key: str) -> PhosonRepl | None:
        """The session's ``PhosonRepl`` if it already exists, without building it."""
        return self._repls.get(key)

    def built_repls(self) -> list[PhosonRepl]:
        """The already-built ``PhosonRepl`` instances (the rest are `_pending`).

        Whatever changes on disk and affects the whole engine (the `config.toml`
        plugins) has to reach *all* live sessions: each one has its own engine with
        its own tool catalog.
        """
        return list(self._repls.values())

    def session_configs(self) -> list[PhosonConfig]:
        """Each live session's own config, without building any engine.

        These are the copies from `create()` (one per session, made when it was
        opened), so a session opened *before* a change but not yet built would start
        with the old state if it were not mirrored too.
        """
        return [cfg for cfg, _sink, _confirm in self._pending.values()] + [
            repl.config for repl in self._repls.values()
        ]

    def config_for(self, key: str) -> PhosonConfig:
        """A session's live config WITHOUT building its engine.

        With the engine built it is ``repl.config``; otherwise, the copy reserved in
        ``create()`` — the same one the ``PhosonRepl`` will receive when it gets
        built, so changes made here (e.g. `config.set` during onboarding) are the
        ones the engine will see when it starts.
        """
        repl = self._repls.get(key)
        if repl is not None:
            return repl.config
        parts = self._pending.get(key)
        if parts is None:
            raise KeyError(f"sesión desconocida: {key}")
        return parts[0]

    # -- confirmations --------------------------------------------------------
    async def request_user(
        self, session_key: str, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._confirmations[request_id] = future
        self._emit(
            "confirm.request",
            {
                "sessionId": session_key,
                "requestId": request_id,
                "kind": kind,
                **payload,
            },
        )
        return await future

    def resolve_confirm(self, request_id: str, decision: dict[str, Any]) -> bool:
        future = self._confirmations.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(decision)
            return True
        return False


class _WatchdogHandle:
    """Watchdog for an in-flight RPC: timer, emitted warnings and cancellation.

    The timer runs in a ``threading.Timer`` and not in an asyncio task on purpose:
    the bug that motivated this **froze the event loop itself** (a synchronous
    call to `git`), so a loop task would never have gotten to run. The thread
    does. The timer is rescheduled while the silence lasts, so the *current*
    one must be cancellable when the RPC finishes.
    """

    def __init__(
        self, bridge: "Bridge", method: str, params: Any, started: float
    ) -> None:
        self._bridge = bridge
        self._method = method
        self._params = params
        self._started = started
        self._session_id = (
            str(params.get("sessionId") or "") if isinstance(params, dict) else ""
        )
        # Lines written for this session when the handler started: anything above
        # this means the engine has spoken (what was emitted earlier, such as
        # `session.turn.started`, goes under another key and does not count).
        self._baseline = bridge._flush_seq.get(self._session_id, 0)
        self._warns = 0
        self._timer: threading.Timer | None = None
        self._cancelled = False
        self._arm()

    def _arm(self, delay: float | None = None) -> None:
        if delay is None:
            delay = _watchdog_delays(self._method)[0]
        timer = threading.Timer(delay, self._fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def cancel(self) -> None:
        """The RPC finished: no need to keep watching."""
        self._cancelled = True
        if self._timer is not None:
            self._timer.cancel()

    def _fire(self) -> None:
        if self._cancelled:
            return
        warned, delay = self._bridge._watchdog_fire(
            self._method, self._params, self._started, self._baseline
        )
        if warned:
            self._warns += 1
        if delay is None or self._warns >= _WATCHDOG_MAX_WARNINGS or self._cancelled:
            return
        self._arm(delay)  # still unresolved: reschedule for later


class Bridge:
    def __init__(self, protocol_out: TextIO) -> None:
        self._started = time.monotonic()
        self._session_list_cache: tuple[float, dict[str, Any]] | None = None
        self._session_list_task: asyncio.Task[dict[str, Any]] | None = None
        self._stt_status_cache: tuple[float, dict[str, Any]] | None = None
        # Model catalog per provider (network) + single-flight.
        self._models_cache: dict[str, tuple[float, list]] = {}
        self._models_task: dict[str, asyncio.Task[tuple[list, str | None]]] = {}
        # Last notification emitted per session: the "sign of life" the watchdog
        # checks to tell a slow engine from a frozen one.
        # When the last line of each session was WRITTEN, and how many have been
        # written. Only a real line on the stream proves the engine is still alive
        # (a queued one does not: with the event loop frozen nobody drains it).
        # The **counter** decides whether there was output: `time.monotonic()` on
        # Windows has ~1 ms resolution and two events in the same ms tie, which
        # made the time-based comparison fail.
        self._last_flush: dict[str, float] = {}
        self._flush_seq: dict[str, int] = {}
        # Token coalescing (see `_TOKEN_BATCH_SECONDS`): content accumulated per
        # session and the timer that schedules the flush.
        self._loop = asyncio.get_running_loop()
        self._token_batch: dict[str, dict[str, str]] = {}
        self._token_timer: dict[str, asyncio.TimerHandle] = {}
        # Serializes writes to the protocol stream: the loop's writer and the
        # watchdog thread can write at the same time.
        self._out_lock = threading.Lock()
        self._out_stream = protocol_out
        self._out: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.config = load_config()
        self.sessions = SessionManager(self.config, self._emit)
        self.sessions.on_wake_turn_done = self._after_wake_turn
        self.stt = SttManager(self._emit)
        # *Reserved* initial session: the `PhosonRepl` is built in a thread the
        # first time someone needs it (`SessionManager.get`), so that the sidecar
        # starts reading stdin without paying for the engine startup.
        self._default_session = self.sessions.create()
        # …and it is warmed up right away, in the background: that way `initialize`
        # does not pay those seconds if it arrives once the engine is already built.
        self._warm_task: asyncio.Task[Any] = asyncio.create_task(self._warm_default())
        self._methods = {
            "system.ping": self._system_ping,
            "initialize": self._initialize,
            "session.new": self._session_new,
            "session.list": self._session_list,
            "peers.start": self._peers_start,
            "peers.list": self._peers_list,
            "peers.send": self._peers_send,
            "session.open": self._session_open,
            "session.close": self._session_close,
            "session.delete": self._session_delete,
            "turn.run": self._turn_run,
            "turn.cancel": self._turn_cancel,
            "session.undo": self._session_undo,
            "session.rewind": self._session_rewind,
            "session.jumpCandidates": self._session_jump_candidates,
            "session.compact": self._session_compact,
            "session.planCompact": self._session_plan_compact,
            "model.set": self._model_set,
            "provider.set": self._provider_set,
            "models.list": self._models_list,
            "config.get": self._config_get,
            "config.set": self._config_set,
            "auth.status": self._auth_status,
            "auth.login": self._auth_login,
            "auth.logout": self._auth_logout,
            "fs.cwd": self._fs_cwd,
            "fs.list": self._fs_list,
            "fs.setCwd": self._fs_set_cwd,
            "fs.read": self._fs_read,
            "fs.readImage": self._fs_read_image,
            "upload.file": self._upload_file,
            "fs.write": self._fs_write,
            "mcp.get": self._mcp_get,
            "mcp.save": self._mcp_save,
            "mcp.remove": self._mcp_remove,
            "mcp.catalog": self._mcp_catalog,
            "plugins.get": self._plugins_get,
            "plugins.set": self._plugins_set,
            "plugins.install": self._plugins_install,
            "plugins.installExtra": self._plugins_install_extra,
            "attachment.list": self._attachment_list,
            "attachment.push": self._attachment_push,
            "attachment.remove": self._attachment_remove,
            "attachment.add": self._attachment_add,
            "attachment.clear": self._attachment_clear,
            "confirm.respond": self._confirm_respond,
            "stt.status": self._stt_status,
            "stt.start": self._stt_start,
            "stt.stop": self._stt_stop,
            "stt.config.set": self._stt_config_set,
            "stt.download": self._stt_download,
            "perf": self._perf,
            "shutdown": self._shutdown,
        }

    # ── Emission (synchronous from the sink, non-blocking) ────────────────
    def _emit(self, method: str, params: dict[str, Any], *, life: bool = True) -> None:
        """Queue a notification. ``life=False`` for those emitted by the bridge
        itself (e.g. `session.turn.started`): they do not prove the engine is alive
        and must not silence the watchdog."""
        session_id = str(params.get("sessionId") or "")
        # Token coalescing: the event is accumulated and flushed when the
        # `_TOKEN_BATCH_SECONDS` window expires. Any other notification from the
        # SAME session flushes the buffer first: tokens must reach the UI before
        # the events that close them (`session.assistant.done`) or surround them
        # (`ui.flush`), or the text would end up out of order.
        if method == "agent.event":
            event = params.get("event")
            etype = event.get("type") if isinstance(event, dict) else None
            if etype in _TOKEN_EVENT_TYPES:
                content = event.get("content") if isinstance(event, dict) else ""
                self._buffer_token(session_id, etype, str(content or ""))
                return
            self._flush_tokens(session_id)
        elif session_id:
            self._flush_tokens(session_id)
        self._out.put_nowait(
            (
                session_id if life else "-",
                dump({"jsonrpc": "2.0", "method": method, "params": params}),
            )
        )

    def _buffer_token(self, session_id: str, etype: str, content: str) -> None:
        """Accumulate a token in its session's batch and schedule the flush."""
        if not content:
            return
        batch = self._token_batch.setdefault(session_id, {})
        batch[etype] = batch.get(etype, "") + content
        if session_id not in self._token_timer:
            self._token_timer[session_id] = self._loop.call_later(
                _TOKEN_BATCH_SECONDS, self._flush_tokens, session_id
            )

    def _flush_tokens(self, session_id: str) -> None:
        """Flush a session's pending token batch (timer or event)."""
        handle = self._token_timer.pop(session_id, None)
        if handle is not None:
            handle.cancel()
        batch = self._token_batch.pop(session_id, None)
        if not batch:
            return
        for etype, content in batch.items():
            line = dump(
                {
                    "jsonrpc": "2.0",
                    "method": "agent.event",
                    "params": {
                        "sessionId": session_id,
                        "event": {"type": etype, "content": content},
                    },
                }
            )
            # Engine liveness: these are real agent output (the UI relies on them
            # for the "Thinking…" indicator).
            self._out.put_nowait((session_id, line))

    def _send(self, message: dict[str, Any]) -> None:
        # `"-"` = no session: an RPC response counts as liveness for none.
        self._out.put_nowait(("-", dump(message)))

    async def _writer(self) -> None:
        while True:
            session_id, line = await self._out.get()
            self._last_flush[session_id] = time.monotonic()
            self._flush_seq[session_id] = self._flush_seq.get(session_id, 0) + 1
            with self._out_lock:
                self._out_stream.write(line + "\n")
                self._out_stream.flush()

    async def _warm_default(self) -> None:
        """Build the default session's engine while there are no RPCs.

        The same build `initialize` would do, but with nobody waiting for it: if the
        first request arrives once it is done, it does not pay for it.
        """
        if not has_configured_provider(self.config):
            # Without a usable provider the build would fail (`build_chat`
            # requires the active provider's key): onboarding writes config.toml
            # first and the engine is built later, on the first turn.
            log.info(
                "precalentado omitido: sin proveedor configurado (onboarding pendiente)"
            )
            return
        try:
            await self.sessions.get(self._default_session)
        except Exception:  # noqa: BLE001 — a failed warm-up breaks nothing
            log.exception("precalentado del engine falló")

    # ── Main loop ─────────────────────────────────────────────────────────
    async def serve(self) -> None:
        writer = asyncio.create_task(self._writer())
        try:
            while True:
                raw = await asyncio.to_thread(sys.stdin.readline)
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("línea no-JSON ignorada: %r", raw[:200])
                    continue
                # Valid JSON but not an object (e.g. `[1,2]`): `_dispatch` would do
                # `message.get` and raise before the try, losing the error (the
                # task is fire-and-forget).
                if not isinstance(message, dict):
                    log.warning("línea JSON no-objeto ignorada: %r", raw[:200])
                    continue
                asyncio.create_task(self._dispatch(message))
        finally:
            writer.cancel()
            # Close the microphone before tearing down the loop (otherwise
            # `asyncio.run` cancels the captures abruptly and may leave audio
            # half-done).
            await self.stt.stop_all()
            await self.sessions.close_all()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        handler = self._methods.get(method)
        if handler is None:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"método desconocido: {method}",
                    },
                }
            )
            return
        if method == "turn.run":
            # `life=False`: the bridge emits it, not the engine; it proves no liveness.
            self._emit(
                "session.turn.started",
                {"sessionId": params.get("sessionId"), "task": params.get("text", "")},
                life=False,
            )
        # Watchdog (separate thread): warns if the RPC goes silent.
        started = time.monotonic()
        watchdog = _WatchdogHandle(self, method, params, started)
        try:
            _t = time.perf_counter()
            result = await handler(params)
            _ms = (time.perf_counter() - _t) * 1000
            # Performance trace: always initialize/turn.run; the rest only if it is
            # slow (avoids noise on stderr).
            if method in ("initialize", "turn.run") or _ms >= 50:
                log.info("[perf] %s %.0fms", method, _ms)
            # A mutation (saved turn, deletion…) invalidates the cached list.
            if method in _SESSION_MUTATING:
                self._session_list_cache = None
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
            if method in ("turn.run", "model.set", "provider.set"):
                self._emit("session.metrics", await self._metrics(params["sessionId"]))
        except Exception as exc:  # noqa: BLE001 — the bridge must never crash
            log.exception("fallo en %s", method)
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
        finally:
            watchdog.cancel()

    def _watchdog_fire(
        self, method: str, params: Any, started: float, baseline: int
    ) -> tuple[bool, float | None]:
        """Decide whether to warn and how long to wait until the next check.

        Returns ``(warned, delay)``; the delay is ``None`` when there is no need to
        keep watching. Runs in the watchdog thread and writes **directly** to the
        protocol stream (with the same lock as `_writer`): with the event loop
        frozen the `_out` queue is not drained and the warning would never reach
        the app.

        "Alive" = there was a notification **from the engine** queued *and* written
        after the handler started. Both are needed: with the loop frozen, what is
        queued never gets written, and what was emitted before the handler started
        (e.g. `session.turn.started`, from the bridge itself) does not prove the
        engine is still there.
        """
        session_id = (
            str(params.get("sessionId") or "") if isinstance(params, dict) else ""
        )
        t_start, t_silence = _watchdog_delays(method)
        now = time.monotonic()

        if self._flush_seq.get(session_id, 0) <= baseline:
            # Silent start: the handler has not written anything yet.
            elapsed = now - started
            if elapsed < t_start:
                return False, t_start - elapsed
            self._warn(method, session_id, elapsed)
            return True, t_start

        # It wrote and went quiet: only warn if the silence is prolonged.
        silent_for = now - self._last_flush.get(session_id, started)
        if silent_for < t_silence:
            return False, t_silence - silent_for
        self._warn(method, session_id, silent_for)
        return True, t_silence

    def _warn(self, method: str, session_id: str, seconds: float) -> None:
        log.warning("watchdog: %s en silencio (%.0fs)", method, seconds)
        line = dump(
            {
                "jsonrpc": "2.0",
                "method": "notify",
                "params": {
                    "sessionId": session_id,
                    "kind": "warn",
                    "message": (
                        f"El motor lleva {seconds:.0f} s sin enviar nada ({method}). "
                        "Si crees que se ha colgado, cancela el turno o reinicia "
                        "el motor desde Ajustes."
                    ),
                },
            }
        )
        try:
            with self._out_lock:
                self._out_stream.write(line + "\n")
                self._out_stream.flush()
        except Exception:  # noqa: BLE001 — a warning must never break the sidecar
            log.exception("watchdog: no se pudo escribir el aviso")

    # ── Helpers ───────────────────────────────────────────────────────────
    async def _system_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Minimal health check: does not build the engine or touch providers.

        Rust creates the process if it does not exist when it receives this RPC; the
        frontend uses it with backoff before reopening the sessions of a sidecar
        that exited.
        """
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptimeSec": round(time.monotonic() - self._started, 3),
        }

    async def _perf(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Sidecar resource usage (RSS + CPU), for measuring from the app."""
        try:
            import resource

            maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux: KB · macOS: bytes.
            rss_bytes = int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024
        except Exception:  # noqa: BLE001 — never break the UI just to measure
            rss_bytes = 0
        times = os.times()
        return {
            "pid": os.getpid(),
            "rssBytes": rss_bytes,
            "cpuUserSec": times.user,
            "cpuSystemSec": times.system,
            "uptimeSec": round(time.monotonic() - self._started, 3),
        }

    async def _metrics(self, session_key: str) -> dict[str, Any]:
        repl = await self.sessions.get(session_key)
        metrics = repl.session_metrics
        tokens = metrics.total_input_tokens + metrics.total_output_tokens
        return {
            # The frontend routes notifications by `sessionId`: without it, the
            # metrics are discarded (and the selected model is not updated).
            "sessionId": session_key,
            "costUsd": metrics.total_cost_usd,
            "credits": metrics.total_credits,
            "tokens": tokens,
            "inputTokens": metrics.total_input_tokens,
            "outputTokens": metrics.total_output_tokens,
            "steps": metrics.step_count,
            "contextTokens": repl._controller.context_tokens,
            "contextWindow": repl._controller.context_window,
            "model": repl.current_model,
            "provider": repl.config.provider,
            "isRunning": repl.is_running,
        }

    # ── RPC methods ───────────────────────────────────────────────────────
    async def _initialize(self, _params: dict[str, Any]) -> dict[str, Any]:
        # Recover the default session if the front-end closed it.
        self._default_session = self.sessions.ensure_any()
        # First-run gate, mirroring the CLI startup (`phoson-cli --setup`, see
        # phoson_cli/__main__.py): without a usable provider (neither
        # `~/.phoson/config.toml` nor keys in the environment) the engine CANNOT
        # be built — `build_chat` requires the active provider's credential and
        # `initialize` would fail before it could even ask for it. We respond
        # with the base config and `onboarding.needed`: the app's wizard collects
        # the key, persists it via `config.set` (writes `~/.phoson/config.toml`)
        # and the engine is built later, lazily, on the first `turn.run`.
        if not has_configured_provider(self.config):
            cfg = self.config
            return {
                "config": {
                    "model": cfg.model,
                    "provider": cfg.provider,
                    "theme": cfg.theme,
                    "sessionsDir": str(cfg.sessions_dir),
                    "safeMode": cfg.safe_mode,
                },
                # Without an engine there is no tool/command catalog: the UI will
                # show them empty until onboarding finishes.
                "tools": {"visible": [], "maskedCount": 0},
                "commands": [],
                "onboarding": {
                    "needed": True,
                    "providers": self._provider_status(cfg),
                    "configExists": _config_path().exists(),
                },
                "defaultSessionId": self._default_session,
                "cwd": os.getcwd(),
                "metrics": _blank_metrics(self._default_session, cfg),
            }
        repl = await self.sessions.get(self._default_session)
        visible = engine_visible_tools(repl.engine)
        return {
            "config": {
                "model": repl.current_model,
                "provider": repl.config.provider,
                "theme": repl.config.theme,
                "sessionsDir": str(repl.config.sessions_dir),
                "safeMode": repl.config.safe_mode,
            },
            "tools": {
                "visible": [getattr(t, "name", str(t)) for t in visible],
                "maskedCount": engine_masked_count(repl.engine),
            },
            "commands": [
                {"names": list(spec.names), "help": getattr(spec, "help", "")}
                for spec in repl._controller.command_catalog.specs
            ],
            # The front-end decides whether to launch the first-run onboarding.
            "onboarding": {
                "needed": not has_configured_provider(repl.config),
                "providers": self._provider_status(repl.config),
                "configExists": _config_path().exists(),
            },
            "defaultSessionId": self._default_session,
            "cwd": os.getcwd(),
            "metrics": await self._metrics(self._default_session),
        }

    async def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        peer_name, peer_team = _peer_identity(params)
        key = self.sessions.create(peer_name, peer_team)
        # The process cwd is the sidecar's workspace (one per project).
        # Metrics are sent right away (zeroed, without building the engine): the
        # composer's model chip comes from them, and without this a new session
        # has no model until the first `session.metrics` (first turn).
        return {
            "sessionId": key,
            "cwd": os.getcwd(),
            "metrics": _blank_metrics(key, self.sessions.config_for(key)),
        }

    async def _peers_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Build the named session to claim presence and enable wakes."""
        key = str(params.get("sessionId") or "")
        repl = await self.sessions.get(key)
        name = str(getattr(repl.config, "peer_name", "") or "")
        if not name:
            raise ValueError("la sesión no tiene identidad de agente")
        # PhosonRepl initializes the plugins on construction; this hook is also
        # idempotent and guarantees the heartbeat on engines that defer it.
        for plugin in getattr(repl.engine, "_loaded_plugins", []):
            if getattr(plugin, "name", "") == "phoson-plugin-peers":
                ensure = getattr(plugin, "ensure_started", None)
                if callable(ensure):
                    await cast(Any, ensure())
        return {
            "ok": True,
            "name": name,
            "team": str(getattr(repl.config, "peer_team", "default")),
            "cwd": os.getcwd(),
        }

    async def _peers_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Presence shared across sidecars, without building any engine."""
        from phoson_plugin_peers.storage import PeerStore, normalize_name

        team = normalize_name(str(params.get("team") or "default"), "team name")
        store = PeerStore(self.config.peers_data_dir, team)
        present = await asyncio.to_thread(store.peers, include_stale=True)
        return {
            "team": team,
            "peers": [
                {
                    "name": peer.name,
                    "cwd": peer.cwd,
                    "state": peer.state,
                    "waitingOn": peer.waiting_on,
                    "live": peer.is_live(store.stale_seconds),
                    "heartbeat": peer.heartbeat,
                }
                for peer in present
            ],
        }

    async def _peers_send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fire-and-forget message from one agent to another, waking the recipient."""
        from phoson_plugin_peers.storage import (
            PeerError,
            PeerStore,
            PeerMessage,
            normalize_name,
        )

        team = normalize_name(str(params.get("team") or "default"), "team name")
        sender = normalize_name(str(params.get("sender") or ""))
        recipient = normalize_name(str(params.get("recipient") or ""))
        content = str(params.get("content") or "").strip()
        if sender == recipient:
            raise PeerError("un agente no puede enviarse un mensaje a sí mismo")
        if not content:
            raise ValueError("el mensaje no puede estar vacío")
        store = PeerStore(self.config.peers_data_dir, team)
        if store.get(recipient) is None:
            # Creating the mailbox allows leaving messages even before the
            # recipient's first startup; its plugin consumes them when it initializes.
            store.mailbox_dirs(recipient)
        message = PeerMessage(
            id=uuid.uuid4().hex,
            sender=sender,
            recipient=recipient,
            content=content,
        )
        await asyncio.to_thread(store.deliver, message)
        return {
            "ok": True,
            "id": message.id,
            "delivered": store.live(recipient) is not None,
        }

    async def _session_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        # FULL history: all saved sessions, from any workspace. Sidecars share
        # `sessions_dir`, so any of them can list them; each `SessionMeta`'s `cwd`
        # allows opening it in its own sidecar (routed by workspace in the
        # frontend).
        #
        # Listing is expensive (reads and parses every .jsonl). Two protections:
        #  - TTL cache (bursts separated in time);
        #  - **single-flight**: `serve()` dispatches in concurrent tasks, so
        #    several simultaneous calls share ONE single in-flight read.
        cached = self._session_list_cache
        if cached is not None and (time.monotonic() - cached[0]) < _SESSION_LIST_TTL:
            return cached[1]
        if self._session_list_task is None:
            self._session_list_task = asyncio.create_task(self._compute_session_list())
        task = self._session_list_task
        try:
            return await task
        finally:
            if self._session_list_task is task:
                self._session_list_task = None

    async def _compute_session_list(self) -> dict[str, Any]:
        sessions = await self.sessions.storage().list_sessions()
        payload = {"sessions": [to_jsonable(s) for s in sessions]}
        self._session_list_cache = (time.monotonic(), payload)
        return payload

    async def _session_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delete a saved session (removes its JSONL file).

        Does not affect sessions open in memory: the frontend removes it from the
        list of saved sessions.
        """
        session_id = str(params.get("id") or "")
        if not session_id:
            raise ValueError("falta el id de la sesión a eliminar")
        await self.sessions.storage().delete(session_id)
        return {"ok": True, "id": session_id}

    async def _session_open(self, params: dict[str, Any]) -> dict[str, Any]:
        peer_name, peer_team = _peer_identity(params)
        key = self.sessions.create(peer_name, peer_team)
        repl = await self.sessions.get(key)
        ok = await repl.load_session(params["id"])
        if not ok:
            await self.sessions.close(key)
            raise ValueError(f"no se pudo cargar la sesión {params['id']}")

        # Honor the working directory the session was created with: tools
        # resolve relative paths against the PROCESS cwd.
        session_cwd = str(getattr(repl._controller.tree, "cwd", "") or "")
        adopted = os.getcwd()
        if session_cwd and os.path.isdir(session_cwd):
            os.chdir(session_cwd)
            adopted = session_cwd
        elif session_cwd:
            log.warning(
                "la sesión %s apunta a un cwd inexistente: %s",
                params["id"],
                session_cwd,
            )

        self._emit("session.info", {"sessionId": key, "cwd": adopted})
        return {
            "sessionId": key,
            "cwd": adopted,
            "sessionCwd": session_cwd,
            "cwdMissing": bool(session_cwd) and not os.path.isdir(session_cwd),
            # The engine is already built here, so the metrics (and with them the
            # chip's model) come for free: without this, a reopened session had no
            # model until the first turn.
            "metrics": await self._metrics(key),
        }

    async def _session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        await self.sessions.close(params["sessionId"])
        # Never leave the bridge without sessions: the front-end may close them all.
        self._default_session = self.sessions.ensure_any()
        return {"ok": True, "defaultSessionId": self._default_session}

    async def _turn_run(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        for path in params.get("attachments") or []:
            repl._controller.attachments.attach(path)
        outcome = await repl.run_turn(params["text"])
        self._emit(
            "session.assistant.done",
            {
                "sessionId": params["sessionId"],
                "status": outcome.status,
                "errorCode": outcome.error_code,
                "finalContent": outcome.final_content,
            },
        )
        return {
            "status": outcome.status,
            "errorCode": outcome.error_code,
            "finalContent": outcome.final_content,
        }

    def _after_wake_turn(self, session_id: str) -> None:
        """Metrics after an autonomous turn (same as after `turn.run`)."""

        async def _emit_metrics() -> None:
            try:
                self._emit("session.metrics", await self._metrics(session_id))
            except Exception:  # noqa: BLE001 — a metric never takes down the bridge
                log.debug("métricas tras wake fallaron", exc_info=True)

        asyncio.create_task(_emit_metrics())

    async def _turn_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "cancelled": (await self.sessions.get(params["sessionId"])).cancel_current()
        }

    @staticmethod
    def _history_messages(repl: Any) -> list[Any]:
        """Messages on the active path, so the UI refreshes after mutating the
        history (undo/rewind/compact). They are returned in the RPC RESPONSE and not
        as a notification: that way the frontend applies them when the call
        resolves, without racing an immediate send."""
        path = repl._controller._node_path()
        return [to_jsonable(n.message) for n in path]

    async def _session_undo(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        ok, message = repl.undo_last_turn()
        return {
            "ok": ok,
            "message": message,
            "history": self._history_messages(repl) if ok else None,
        }

    async def _session_rewind(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        ok, message = repl.jump_to_user_turn(params["userNodeId"])
        return {
            "ok": ok,
            "message": message,
            "history": self._history_messages(repl) if ok else None,
        }

    async def _session_jump_candidates(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        return {
            "candidates": [
                {"userNodeId": nid, "preview": txt}
                for nid, txt in repl.jump_candidates()
            ]
        }

    async def _session_compact(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        before, after, ok = await repl.compact_context(params.get("profile"))
        return {
            "ok": ok,
            "before": before,
            "after": after,
            "history": self._history_messages(repl) if ok else None,
        }

    async def _session_plan_compact(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        return to_jsonable(repl.plan_compaction(params.get("profile")))

    async def _model_set(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        await repl.set_model(params["model"], provider=params.get("provider"))
        return {"ok": True, "model": repl.current_model}

    async def _provider_set(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        await repl.set_provider(params["provider"])
        self._invalidate_models()
        return {"ok": True, "provider": repl.config.provider}

    async def _models_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Live list of models from the active provider (I-113), with overrides.

        Reuses the CLI's `list_available_models`: queries the provider directly and
        applies the overrides from `~/.phoson/models.json`. If the network fails, it
        degrades to the current model (engine behavior).

        Accepts an optional `provider`: Settings requests the catalog of the
        **draft** provider (not yet saved via `config.set`), so the live config is
        cloned with that provider. Without the parameter the active one is used, as
        always.

        Cached per provider (`_MODELS_LIST_TTL`) and single-flight: the picker
        opens it often and the response only changes when the provider's catalog
        changes. A network failure is NOT cached (it retries on the next call).
        """
        # No engine built (onboarding): the session's reserved config is enough
        # — the catalog is a network query to the provider, it uses no tools.
        session_id = str(params.get("sessionId") or "")
        cfg = self.sessions.config_for(session_id)
        requested = str(params.get("provider") or "").strip()
        if requested and requested.lower() != str(cfg.provider).lower():
            cfg = _config_for_provider(cfg, requested)
        repl = self.sessions.repl_if_built(session_id)
        # The repl's "current" model is only valid for its own provider; with a
        # different `provider` requested, the marker is that provider's default
        # model (the one the config clone carries).
        repl_matches = repl is not None and _provider_of(repl.config) == _provider_of(
            cfg
        )
        current = {
            "model": repl.current_model
            if repl is not None and repl_matches
            else cfg.model,
            "provider": cfg.provider,
        }
        key = f"{cfg.provider}|{getattr(cfg, 'base_url', '') or ''}"

        cached = self._models_cache.get(key)
        if cached is not None and (time.monotonic() - cached[0]) < _MODELS_LIST_TTL:
            return {"current": current, "models": cached[1], "cached": True}

        task = self._models_task.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_models(cfg))
            self._models_task[key] = task
        try:
            models, error = await task
        finally:
            if self._models_task.get(key) is task:
                self._models_task.pop(key, None)

        if error is not None:
            return {"current": current, "models": [], "error": error}
        self._models_cache[key] = (time.monotonic(), models)
        return {"current": current, "models": models}

    async def _fetch_models(self, cfg: Any) -> tuple[list, str | None]:
        """Serialized provider catalog, or `(None, error)` on failure."""
        from phoson_cli.model_selector import list_available_models

        try:
            options = await list_available_models(cfg)
        except Exception as exc:  # noqa: BLE001 — the UI must never crash
            log.warning("models.list falló: %s", exc)
            return [], str(exc)
        return [to_jsonable(o) for o in options], None

    def _invalidate_models(self) -> None:
        """The catalog depends on provider/base_url: a change invalidates it."""
        self._models_cache.clear()

    # ── Configuration ─────────────────────────────────────────────────────
    # Fields the GUI can write (safe to persist).
    _SAFE_FIELDS = (
        "provider",
        "model",
        "subagent_model",
        "reasoning_effort",
        "safe_mode",
        "theme",
        "notify_on_completion",
        "enable_mcp",
    )
    #: provider id -> config fields (key and/or base_url), in UI display order.
    #: `bedrock` has no field of its own: it uses the AWS credential chain.
    #: `ollama`/`lmstudio` only need base_url.
    _PROVIDERS: dict[str, dict[str, str]] = {
        "openrouter": {"key": "openrouter_api_key"},
        "openai": {"key": "openai_api_key"},
        # Subscriptions (OAuth): no field in config.toml, the token lives in
        # `~/.phoson/auth*.json` and is managed by `phoson_llm.auth`.
        "chatgpt": {},
        "anthropic": {"key": "anthropic_api_key"},
        "ollama": {"base_url": "ollama_base_url"},
        "github": {"key": "github_token"},
        "nvidia": {"key": "nvidia_api_key"},
        "xai": {"key": "xai_api_key"},
        "groq": {"key": "groq_api_key"},
        "deepseek": {"key": "deepseek_api_key"},
        "alibaba": {"key": "alibaba_api_key"},
        "together": {"key": "together_api_key"},
        "perplexity": {"key": "perplexity_api_key"},
        "lmstudio": {"base_url": "lmstudio_base_url"},
        "vllm": {"key": "vllm_api_key", "base_url": "vllm_base_url"},
        "azure": {"key": "azure_openai_api_key"},
        "gemini": {"key": "gemini_api_key"},
        "google-code-assist": {},
        "mistral": {"key": "mistral_api_key"},
        "bedrock": {},
        "fireworks": {"key": "fireworks_api_key"},
        "cohere": {"key": "cohere_api_key"},
        "omniroute": {"key": "omniroute_api_key", "base_url": "omniroute_base_url"},
    }

    def _provider_status(self, cfg: Any) -> list[dict[str, Any]]:
        """Status of each provider.

        Indicates whether it has a key (and its source, **never the value**), and
        the configured `base_url` when the provider supports one (it is not secret).

        Subscription providers (`chatgpt`, `google-code-assist`) have no key in
        `config.toml`: their credential is the OAuth login managed by
        `phoson_llm.auth`, so `hasKey` reflects whether there is a session and the
        details are added (`auth`, `loggedIn`, `account`, `plan`).
        """
        sources = dict(getattr(cfg, "_secret_sources", {}) or {})
        out: list[dict[str, Any]] = []
        for pid, fields in self._PROVIDERS.items():
            key_field = fields.get("key")
            url_field = fields.get("base_url")
            entry: dict[str, Any] = {
                "id": pid,
                "hasKey": bool(getattr(cfg, key_field, None)) if key_field else False,
                "source": (
                    sources.get(key_field, "default") if key_field else "default"
                ),
                "supportsKey": bool(key_field),
                "supportsBaseUrl": bool(url_field),
                "baseUrl": (getattr(cfg, url_field, None) or "") if url_field else "",
            }
            if pid in _AUTH_PROVIDERS:
                status = self._auth_provider_status(pid)
                entry.update(
                    {
                        # The credential exists (stored token), even though it is not a
                        # key: that way onboarding and the provider selector treat it
                        # the same as a configured key.
                        "hasKey": bool(status.get("loggedIn")),
                        "source": "file" if status.get("loggedIn") else "default",
                        "auth": True,
                        "loggedIn": bool(status.get("loggedIn")),
                        "account": status.get("email"),
                        "plan": status.get("plan"),
                        "expiresAt": status.get("expiresAt"),
                    }
                )
            out.append(entry)
        return out

    def _auth_provider_status(self, provider: str) -> dict[str, Any]:
        """Summary of a provider's OAuth login (without touching the network)."""
        module_name = _AUTH_PROVIDERS.get(provider)
        if module_name is None:
            return {}
        try:
            module = __import__(module_name, fromlist=["auth_status"])
            status = module.auth_status()
        except Exception as exc:  # noqa: BLE001 — auth failure won't break the panel
            log.warning("auth.status(%s) falló: %s", provider, exc)
            return {"loggedIn": False, "error": str(exc)}
        return {
            "loggedIn": bool(getattr(status, "logged_in", False)),
            "email": getattr(status, "email", None),
            "plan": getattr(status, "plan_type", None),
            "expiresAt": getattr(status, "expires_at", None),
        }

    async def _auth_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Status of the subscription providers (all of them by default)."""
        only = params.get("provider")
        providers = [str(only)] if only else list(_AUTH_PROVIDERS)
        return {
            "providers": [
                {"provider": pid, **self._auth_provider_status(pid)}
                for pid in providers
                if pid in _AUTH_PROVIDERS
            ]
        }

    async def _auth_login(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start an OAuth login with a subscription provider.

        Two flows, chosen with `method`:
          * `device`  (ChatGPT): shows a code and polls until the user approves
            it — opens no ports, also works over SSH.
          * `browser` (both): ephemeral loopback server + system browser.

        The UI opens the URL that arrives in `auth.prompt` (the sidecar does not
        open the browser: in a desktop app the front controls that). The RPC
        finishes when the login completes, so the front can await the result and
        refresh the status.
        """
        provider = str(params.get("provider") or "")
        method = str(params.get("method") or "browser")
        if provider not in _AUTH_PROVIDERS:
            raise ValueError(f"proveedor sin login OAuth: {provider!r}")

        def prompt(payload: dict[str, Any]) -> None:
            self._emit(
                "auth.prompt",
                {"provider": provider, **payload},
                life=False,
            )

        if provider == "chatgpt":
            from phoson_llm.auth import chatgpt

            if method == "browser":
                await chatgpt.login_browser(
                    open_browser=False,
                    on_url=lambda url: prompt({"kind": "url", "url": url}),
                )
            else:
                device = await chatgpt.request_device_code()
                prompt(
                    {
                        "kind": "device",
                        "url": device.verification_url,
                        "code": device.user_code,
                    }
                )
                tokens = await chatgpt.poll_device_code(device)
                chatgpt.save_tokens(tokens)
        else:
            from phoson_llm.auth import code_assist

            # The headless flow (copy/paste a code) needs user input via stdin;
            # the app uses the system browser.
            await code_assist.login_browser(
                open_browser=False,
                on_url=lambda url: prompt({"kind": "url", "url": url}),
            )

        self._invalidate_models()
        status = self._auth_provider_status(provider)
        self._emit("auth.complete", {"provider": provider, **status}, life=False)
        return {"ok": True, "provider": provider, "status": status}

    async def _auth_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delete the provider's OAuth token (does not touch config.toml)."""
        provider = str(params.get("provider") or "")
        module_name = _AUTH_PROVIDERS.get(provider)
        if module_name is None:
            raise ValueError(f"proveedor sin login OAuth: {provider!r}")
        module = __import__(module_name, fromlist=["clear_tokens"])
        removed = bool(module.clear_tokens())
        self._invalidate_models()
        return {"ok": True, "provider": provider, "removed": removed}

    async def _config_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Effective config for the panel. Secrets are NEVER sent: only their
        source (`file`/`env`/`default`) and whether they are present."""
        from phoson_cli.config import (
            has_configured_provider,
            enabled_providers_from_config,
        )

        # Without building the engine: `config.get` also serves onboarding, which
        # runs precisely when the engine cannot be built yet.
        cfg = self.sessions.config_for(str(params.get("sessionId") or ""))
        return {
            "provider": cfg.provider,
            "model": cfg.model,
            "subagentModel": cfg.subagent_model,
            "reasoningEffort": cfg.reasoning_effort,
            "theme": cfg.theme,
            "safeMode": cfg.safe_mode,
            "notifyOnCompletion": cfg.notify_on_completion,
            "sessionsDir": str(cfg.sessions_dir),
            "enabledProviders": enabled_providers_from_config(
                cfg, for_persistence=True
            ),
            "providers": self._provider_status(cfg),
            "hasProvider": has_configured_provider(cfg),
            "enableMcp": bool(getattr(cfg, "enable_mcp", False)),
            "mcpConfigFile": str(getattr(cfg, "mcp_config_file", "")),
        }

    async def _config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Persist a patch of safe fields and, optionally, API keys
        (write-only: they go in, never come out)."""
        from phoson_cli.config import save_config

        # Without building the engine: `config.set` is what writes the first
        # `~/.phoson/config.toml` during onboarding — precisely when the engine
        # cannot be built yet (there is no key to validate).
        session_id = str(params.get("sessionId") or "")
        cfg = self.sessions.config_for(session_id)
        patch = params.get("patch") or {}
        secrets = params.get("secrets") or {}

        touched: set[str] = set()
        values: dict[str, Any] = {}
        for field in self._SAFE_FIELDS:
            if field in patch:
                values[field] = patch[field]
                touched.add(field)
        for pid, value in secrets.items():
            field = self._PROVIDERS.get(str(pid), {}).get("key")
            if field and value:
                values[field] = value
                touched.add(field)
        for pid, value in (params.get("base_urls") or {}).items():
            field = self._PROVIDERS.get(str(pid), {}).get("base_url")
            if field is not None:
                values[field] = value or None
                touched.add(field)
        for field, value in values.items():
            setattr(cfg, field, value)
            # Mirror into the base config: NEW sessions created in this sidecar
            # inherit the change (without this, a `session.new` after onboarding
            # would start again with no provider or key).
            setattr(self.config, field, value)

        only = set(self._SAFE_FIELDS) | touched | {"enabled_providers"}
        path = save_config(cfg, only_fields=only, explicit_secret_fields=touched)

        # Apply live what the engine needs (client/model/base_url), only if it
        # is already built. If it is not (onboarding), the lazy build of the
        # first `turn.run` will already read this config.
        if self.sessions.is_built(session_id):
            if "provider" in patch or touched:
                try:
                    repl = await self.sessions.get(session_id)
                    await repl.set_provider(cfg.provider)
                except Exception as exc:  # noqa: BLE001 — save must not fail on rebuild
                    log.warning("set_provider tras config.set falló: %s", exc)
            self._emit("session.metrics", await self._metrics(session_id))

        # Provider/base_url/key changes → the cached catalog is no longer valid.
        self._invalidate_models()
        return {"ok": True, "path": str(path), "config": await self._config_get(params)}

    # ── File explorer (workspace) ─────────────────────────────────────────
    #: Noisy directories the explorer skips.
    _NOISE_DIRS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }

    async def _fs_cwd(self, _params: dict[str, Any]) -> dict[str, Any]:
        """The sidecar's working directory (the one the tools see)."""
        return {"cwd": os.getcwd()}

    async def _fs_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """List a directory for the explorer.

        Exposes nothing the engine does not already have: the agent's tools
        operate on the whole filesystem.
        """
        from pathlib import Path

        target = Path(params.get("path") or os.getcwd()).expanduser()
        try:
            target = target.resolve()
        except OSError:
            pass
        if not target.is_dir():
            raise ValueError(f"no es un directorio: {target}")

        entries: list[dict[str, Any]] = []
        with os.scandir(target) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir and entry.name in self._NOISE_DIRS:
                    continue
                stat = None
                try:
                    stat = entry.stat()
                except OSError:
                    pass
                entries.append(
                    {
                        "name": entry.name,
                        "dir": is_dir,
                        "hidden": entry.name.startswith("."),
                        "size": None if (is_dir or stat is None) else stat.st_size,
                        "mtime": None if stat is None else int(stat.st_mtime),
                    }
                )
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {
            "path": str(target),
            "parent": str(target.parent) if target.parent != target else None,
            "entries": entries[:400],
            "truncated": len(entries) > 400,
        }

    async def _fs_set_cwd(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set the working directory of the sidecar process.

        The engine's tools resolve relative paths against the process cwd, so this
        is the agent's real *workspace*. New sessions record this cwd in their
        metadata (`tree.cwd`).
        """
        from pathlib import Path

        target = Path(params["path"]).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"no es un directorio: {target}")
        os.chdir(target)
        # The default session can still adopt the new workspace.
        self._default_session = self.sessions.ensure_any()
        repl = await self.sessions.get(self._default_session)
        repl._controller.tree.cwd = str(target)
        self._emit(
            "session.info", {"sessionId": self._default_session, "cwd": str(target)}
        )
        return {"cwd": str(target)}

    #: Read limit for the code viewer (512 KB).
    _MAX_READ = 512 * 1024

    async def _fs_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a text file for the viewer. Detects binaries by NUL or by a
        UTF-8 decoding failure."""
        import codecs
        from pathlib import Path

        target = Path(params["path"]).expanduser().resolve()
        if not target.is_file():
            raise ValueError(f"no es un archivo: {target}")
        size = target.stat().st_size
        # Only the first `_MAX_READ` bytes: `read_bytes()` would load the ENTIRE
        # file into memory before slicing (a 2 GB log would freeze the sidecar
        # even if the response were small).
        with target.open("rb") as fh:
            raw = fh.read(self._MAX_READ)
        if b"\x00" in raw[:8192]:
            return {
                "path": str(target),
                "binary": True,
                "text": "",
                "size": size,
                "truncated": False,
            }
        # Incremental decoder: if the cut falls in the middle of a UTF-8
        # character, the partial tail is discarded instead of failing (a
        # truncated file must not be reported as binary). Truly invalid bytes
        # do raise and fall back to binary.
        try:
            text = codecs.getincrementaldecoder("utf-8")(errors="strict").decode(raw)
        except UnicodeDecodeError:
            return {
                "path": str(target),
                "binary": True,
                "text": "",
                "size": size,
                "truncated": False,
            }
        return {
            "path": str(target),
            "binary": False,
            "text": text,
            "size": size,
            "truncated": size > self._MAX_READ,
        }

    #: Image extensions the webview can preview.
    _IMAGE_MIME: dict[str, str] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".avif": "image/avif",
    }
    #: Preview limit (base64 weighs ~1.33× in the JSON).
    _MAX_IMAGE_READ = 12 * 1024 * 1024

    async def _fs_read_image(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a local image as base64 to preview it in the webview.

        The webview cannot open system paths, so the content is returned embedded
        (data URL in the frontend). Images only, with a size cap; a relative path
        is resolved against the workspace cwd.
        """
        import base64
        from pathlib import Path

        target = Path(str(params.get("path") or "")).expanduser().resolve()
        if not target.is_file():
            raise ValueError(f"no es un archivo: {target}")
        mime = self._IMAGE_MIME.get(target.suffix.lower())
        if mime is None:
            raise ValueError(
                f"extensión no previsualizable: {target.suffix or '(sin extensión)'}"
            )
        size = target.stat().st_size
        if size > self._MAX_IMAGE_READ:
            raise ValueError(
                f"imagen demasiado grande para previsualizar ({size} bytes, "
                f"máx {self._MAX_IMAGE_READ})"
            )
        data = base64.b64encode(target.read_bytes()).decode("ascii")
        return {"path": str(target), "mediaType": mime, "size": size, "base64": data}

    #: Cap for uploading non-native files to the workspace (50 MB).
    _MAX_UPLOAD = 50 * 1024 * 1024

    async def _upload_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Upload to the workspace a file NOT supported as a native attachment.

        It is written to ``<cwd>/uploads/`` (deduplicating the name) and the
        **relative** path is returned so the prompt can reference it and the agent
        can read it with ``read_file``.
        """
        import base64
        from pathlib import Path

        name = Path(str(params.get("name") or "archivo")).name or "archivo"
        data = str(params.get("data") or "")
        if not data:
            raise ValueError("falta el contenido del archivo")
        try:
            payload = base64.b64decode(data, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"contenido base64 inválido: {exc}") from exc
        if len(payload) > self._MAX_UPLOAD:
            raise ValueError(
                f"archivo demasiado grande ({len(payload)} bytes, "
                f"máx {self._MAX_UPLOAD})"
            )

        workspace = Path(os.getcwd())
        dest_dir = workspace / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        dest.write_bytes(payload)
        try:
            relative = dest.relative_to(workspace).as_posix()
        except ValueError:
            relative = str(dest)
        return {
            "ok": True,
            "path": str(dest),
            "relative": relative,
            "name": dest.name,
            "size": len(payload),
        }

    async def _fs_write(self, params: dict[str, Any]) -> dict[str, Any]:
        """Write a text file (lightweight editor)."""
        from pathlib import Path

        target = Path(params["path"]).expanduser().resolve()
        text = params.get("text", "")
        await asyncio.to_thread(target.write_text, text, encoding="utf-8")
        return {"ok": True, "path": str(target), "size": target.stat().st_size}

    # ── MCP ───────────────────────────────────────────────────────────────
    #: On-disk format: ~/.phoson/mcps.json → {"mcpServers": {name: {...}}}
    def _mcp_load(self, cfg: Any) -> dict[str, Any]:
        from pathlib import Path

        path = Path(getattr(cfg, "mcp_config_file"))
        if not path.exists():
            return {"mcpServers": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 — broken JSON must not break the UI
            log.warning("mcps.json ilegible: %s", exc)
            return {"mcpServers": {}}
        return data if isinstance(data, dict) else {"mcpServers": {}}

    @staticmethod
    def _mcp_servers(data: dict[str, Any]) -> dict[str, Any]:
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = data.get("servers")
        return servers if isinstance(servers, dict) else {}

    def _mcp_store(self, cfg: Any, data: dict[str, Any]) -> str:
        from pathlib import Path

        path = Path(getattr(cfg, "mcp_config_file"))
        path.parent.mkdir(parents=True, exist_ok=True)
        out = dict(data)
        out["mcpServers"] = self._mcp_servers(data)
        out.pop("servers", None)  # normalize to the canonical form
        path.write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return str(path)

    def _mcp_summary(self, cfg: Any) -> dict[str, Any]:
        from pathlib import Path

        try:
            from phoson_plugin_mcp._plugin import MCP_AVAILABLE
        except Exception:  # noqa: BLE001 — plugin not installed
            MCP_AVAILABLE = False

        servers = []
        for name, spec in sorted(self._mcp_servers(self._mcp_load(cfg)).items()):
            spec = spec if isinstance(spec, dict) else {}
            env = spec.get("env") or {}
            headers = spec.get("headers") or {}
            servers.append(
                {
                    "name": name,
                    "transport": spec.get("transport")
                    or ("sse" if spec.get("url") else "stdio"),
                    "command": spec.get("command", "") or "",
                    "args": [str(a) for a in (spec.get("args") or [])],
                    "url": spec.get("url", "") or "",
                    "enabled": bool(spec.get("enabled", True)),
                    # Only the NAMES: the values are usually tokens.
                    "envKeys": sorted(env.keys()) if isinstance(env, dict) else [],
                    "headerKeys": sorted(headers.keys())
                    if isinstance(headers, dict)
                    else [],
                }
            )
        return {
            "enabled": bool(getattr(cfg, "enable_mcp", False)),
            "configPath": str(Path(getattr(cfg, "mcp_config_file"))),
            "sdkAvailable": bool(MCP_AVAILABLE),
            "servers": servers,
        }

    async def _mcp_get(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        return self._mcp_summary(repl.config)

    async def _mcp_save(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create or update an MCP server, and reload the plugins.

        ``env``/``headers`` are merged with what is stored: an empty value
        **keeps** the existing one (the UI never receives the tokens, so when
        editing only the keys arrive), and ``envRemove``/``headersRemove`` delete
        the keys the user removed from the form. That way a server is edited
        without rewriting secrets that cannot be shown.
        """
        repl = await self.sessions.get(params["sessionId"])
        cfg = repl.config
        name = str(params["name"]).strip()
        if not name:
            raise ValueError("el nombre del servidor no puede estar vacío")
        incoming = params.get("server") or {}

        data = self._mcp_load(cfg)
        servers = dict(self._mcp_servers(data))
        spec = dict(servers.get(name) or {})

        for key in ("transport", "command", "url"):
            if key in incoming:
                spec[key] = incoming[key]
        if "args" in incoming:
            spec["args"] = [a for a in incoming["args"] if str(a) != ""]
        if "enabled" in incoming:
            spec["enabled"] = bool(incoming["enabled"])

        for key, remove_key in (("env", "envRemove"), ("headers", "headersRemove")):
            merge_secret_map(spec, incoming, key, remove_key)

        clean_spec(spec)

        servers[name] = spec
        data["mcpServers"] = servers
        path = self._mcp_store(cfg, data)
        await self._reload_plugins(repl)
        return {"ok": True, "path": path, "mcp": self._mcp_summary(cfg)}

    async def _mcp_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        cfg = repl.config
        data = self._mcp_load(cfg)
        servers = dict(self._mcp_servers(data))
        servers.pop(str(params["name"]), None)
        data["mcpServers"] = servers
        path = self._mcp_store(cfg, data)
        await self._reload_plugins(repl)
        return {"ok": True, "path": path, "mcp": self._mcp_summary(cfg)}

    async def _mcp_catalog(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search servers in the official MCP registry (query only).

        Does not touch `mcps.json` or reload plugins: the user adds the server with
        `mcp.save` after filling in its keys. The download runs in a separate
        thread so as not to block the loop (the other sessions stay alive).
        """
        query = str(params.get("query") or "")
        try:
            limit = int(params.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        return await asyncio.to_thread(mcp_catalog.search, query, limit)

    # ── Engine plugins ────────────────────────────────────────────────────
    # Plugin state lives in `config.toml` (`enable_*` flags and/or the
    # `plugins`/`disabled_plugins` lists) and the engine reads it when it is
    # built, so these handlers write with `save_config` and reload the engine —
    # just like `config.set` and `mcp.save`. None of them builds the session to
    # read.

    def _mirror_config_fields(
        self, cfg: Any, fields: list[str], configs: Sequence[Any] = ()
    ) -> None:
        """Copy the touched fields to the base config and to each live session.

        Sessions are born with a `copy.copy` of the base config: lists stay shared
        until someone rebinds them (and then the new list is theirs alone), so
        without this mirror a session opened after the change, or opened before but
        not yet built, would start with the old state.

        `configs` are the per-session configs (``SessionManager.session_configs``,
        which includes those of live sessions and of not-yet-built ones). Those
        that already have an engine loaded the old plugins, so they also need to be
        rebuilt: the caller does that with `_reload_plugins`.
        """
        for target in [self.config, *configs]:
            if target is cfg:
                continue
            for field in fields:
                value = getattr(cfg, field, None)
                setattr(
                    target, field, list(value) if isinstance(value, list) else value
                )

    async def _plugins_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Engine plugin catalog with their current state.

        Does not build the engine: reads the session's live config (like
        `config.get`), so that Settings › Plugins works even before the first turn
        (or with no provider configured).
        """
        from .plugins import catalog, installable

        cfg = self.sessions.config_for(str(params.get("sessionId") or ""))
        can_install, blocked = installable()
        # Installing extras (optional engine dependencies) is a different path:
        # it needs an interpreter with `pip`, not the plugin manager's `uv`.
        can_install_extras, extras_blocked = _extras.installable()
        return {
            "plugins": catalog(cfg),
            "canInstall": can_install,
            "installBlocked": blocked,
            "canInstallExtras": can_install_extras,
            "installExtrasBlocked": extras_blocked,
            "configPath": str(_config_path()),
        }

    async def _plugins_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Enable or disable a plugin and persist the change in `config.toml`.

        Writes the same as `phoson-cli plugin enable/disable` (the config's
        `enable_*` flag or the spec in `plugins`/`disabled_plugins`) and rebuilds
        the engine so the change takes effect live. Returns
        `{"ok": False, "reason": "locked"}` when another source sets the state
        (dictation, peers, a flag that `save_config` does not persist) and the
        toggle cannot change it.
        """
        from .plugins import catalog, set_enabled

        session_id = str(params.get("sessionId") or "")
        cfg = self.sessions.config_for(session_id)
        result = set_enabled(
            cfg, str(params.get("id") or ""), bool(params.get("enabled"))
        )
        fields = list(result.get("fields") or [])
        repls = self.sessions.built_repls()
        if result.get("ok") and fields:
            self._mirror_config_fields(cfg, fields, self.sessions.session_configs())
            # Plugins are set by `config.toml`, which is engine-wide: **all** live
            # sessions have to be rebuilt, not just the one that made the toggle.
            # With just one, the others kept their old catalog and the agent in
            # such a session did not see the newly enabled tool.
            for repl in repls:
                await self._reload_plugins(repl)
        if result.get("ok"):
            result["plugins"] = catalog(cfg)
            tools = self._tools_payload(repls)
            if tools is not None:
                result["tools"] = tools
        return result

    async def _plugins_install(self, params: dict[str, Any]) -> dict[str, Any]:
        """Install a third-party plugin from `source` and enable it.

        A plugin is **Python code that runs with the user's permissions**, so the
        UI confirms the exact source and `confirmed: true` is required here: without
        it nothing runs (a stray request does not install). The work is done by the
        engine's `install_plugin` (it resolves the commit, runs `uv pip install`,
        requires exactly one new entry point and records the lockfile); it is slow,
        hence `plugins.install` has its own watchdog.
        """
        from .plugins import catalog, install, installable

        if not params.get("confirmed"):
            raise ValueError(
                "la instalación de un plugin requiere confirmación explícita"
            )
        can_install, blocked = installable()
        if not can_install:
            return {"ok": False, "reason": blocked}
        source = str(params.get("source") or "").strip()
        if not source:
            raise ValueError("falta el origen del plugin")

        session_id = str(params.get("sessionId") or "")
        cfg = self.sessions.config_for(session_id)
        # In a thread: `install_plugin` runs `uv pip install` blockingly (seconds
        # or minutes) and on the event loop it would freeze the whole sidecar,
        # other sessions included. Its failures are already `PluginManagerError`
        # (actionable message), which `_dispatch` returns as the RPC error.
        name = await asyncio.to_thread(install, cfg, source)
        repls = self.sessions.built_repls()
        self._mirror_config_fields(cfg, ["plugins"], self.sessions.session_configs())
        # Same reason as in `plugins.set`: the newly installed plugin has to
        # reach all live sessions, not just the panel's.
        for repl in repls:
            await self._reload_plugins(repl)
        return {
            "ok": True,
            "name": name,
            "plugins": catalog(cfg),
            "tools": self._tools_payload(repls),
        }

    async def _plugins_install_extra(self, params: dict[str, Any]) -> dict[str, Any]:
        """Install a missing engine **extra** (e.g. `[stt]`, `[computeruse]`).

        An engine plugin can depend on an optional extra: if it is missing, the
        engine loads the plugin but skips it ("Active, but the [stt] extra is
        missing from the engine environment"). This handler fixes that: it
        resolves the extra's requirements from the engine metadata, installs them
        into `~/.phoson/extras/<extra>` and adds them to the sidecar's `sys.path`,
        so the plugin works without restarting the app.

        The installation downloads wheels (onnxruntime, Pillow…): it takes a while,
        so it runs in a thread (the event loop does not freeze) and has its own
        watchdog.
        """
        from .plugins import catalog

        extra = str(params.get("extra") or "").strip()
        if not extra:
            raise ValueError("falta el extra a instalar")
        # In a thread: `pip install` is blocking (seconds or minutes) and on the
        # event loop it would freeze the whole sidecar. The phase (downloading the
        # installer, installing) is emitted to the loop with `call_soon_threadsafe`.
        loop = asyncio.get_running_loop()
        session_id = str(params.get("sessionId") or "")

        def _progress(phase: str) -> None:
            # `life=False`: the bridge emits it; it does not prove the engine is alive.
            loop.call_soon_threadsafe(
                lambda: self._emit(
                    "extras.progress",
                    {"sessionId": session_id, "extra": extra, "phase": phase},
                    life=False,
                )
            )

        result = await asyncio.to_thread(_extras.install_extra, extra, _progress)
        if not result.get("ok"):
            return result
        # The newly usable plugin has to reach all live sessions (same reason
        # as in `plugins.set`).
        repls = self.sessions.built_repls()
        for repl in repls:
            await self._reload_plugins(repl)
        cfg = self.sessions.config_for(session_id)
        result["plugins"] = catalog(cfg)
        result["tools"] = self._tools_payload(repls)
        return result

    # ── Attachments (paste / drag files) ──────────────────────────────────
    #: Size cap when materializing a file sent from the UI.
    _MAX_PUSH = 25 * 1024 * 1024

    def _attachment_payload(self, repl: Any) -> list[dict[str, Any]]:
        """Normalized view of the pending attachments: path, name and kind."""
        from pathlib import Path

        out: list[dict[str, Any]] = []
        for att in repl._controller.attachments.list_pending():
            path = str(getattr(att, "path", ""))
            block = getattr(att, "block", None)
            kind = (
                type(block).__name__.replace("Block", "").lower() if block else "file"
            )
            out.append({"path": path, "name": Path(path).name, "kind": kind})
        return out

    async def _attachment_list(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        return {"attachments": self._attachment_payload(repl)}

    async def _attachment_push(self, params: dict[str, Any]) -> dict[str, Any]:
        """Materialize a file sent by the UI (base64) and attach it.

        Pasting/dragging in the WebView does not provide system paths, so the file
        travels by content and the sidecar writes it to `~/.phoson/attachments/`
        before using `AttachmentManager.attach`.
        """
        import uuid as _uuid
        import base64
        from pathlib import Path

        repl = await self.sessions.get(params["sessionId"])
        raw = base64.b64decode(params.get("data") or "")
        if not raw:
            raise ValueError("archivo vacío")
        if len(raw) > self._MAX_PUSH:
            raise ValueError("archivo demasiado grande (máx. 25 MB)")

        name = Path(str(params.get("name") or "archivo")).name or "archivo"
        dest_dir = Path(repl.config.sessions_dir).expanduser().parent / "attachments"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{_uuid.uuid4().hex[:8]}-{name}"
        dest.write_bytes(raw)
        try:
            repl._controller.attachments.attach(str(dest))
        except Exception:
            dest.unlink(missing_ok=True)  # unsupported type: leave no garbage
            raise
        return {"ok": True, "attachments": self._attachment_payload(repl)}

    async def _attachment_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove a pending attachment.

        `AttachmentManager` has no remove, so the rest are re-attached.
        """
        repl = await self.sessions.get(params["sessionId"])
        manager = repl._controller.attachments
        target = str(params.get("path") or "")
        keep = [str(a.path) for a in manager.list_pending() if str(a.path) != target]
        manager.clear()
        for path in keep:
            manager.attach(path)
        return {"ok": True, "attachments": self._attachment_payload(repl)}

    @staticmethod
    async def _reload_plugins(repl: Any) -> None:
        """Rebuild the engine so the plugins react to the config.

        `set_model(current)` without `reuse_engine` is the path the CLI uses to
        reapply an MCP change (`_reapply_mcp`): it always rebuilds tools and
        plugins. `set_provider` does not work here for two reasons — it reads
        `default_model` from `models.json` (and would overwrite the model chosen in
        Settings) and, if the model does not match the engine's, it stays on the
        fast path and does **not** reload anything, leaving the toggle a no-op.
        """
        try:
            await repl.set_model(repl.current_model)
        except Exception as exc:  # noqa: BLE001 — saving must not fail because of this
            log.warning("recarga de plugins falló: %s", exc)

    @staticmethod
    def _tools_payload(repls: list[Any]) -> dict[str, Any] | None:
        """Tool catalog seen by a built session (``None`` if there is none).

        Plugins are global `config.toml` state (and after the toggle all live
        sessions are rebuilt with the same one), so any built session will do.
        `plugins.set`/`plugins.install` return it so the UI refreshes the list that
        feeds the tool selectors (e.g. the subagents one) instead of keeping the
        one from startup.
        """
        for repl in repls:
            engine = getattr(repl, "engine", None)
            if engine is None:
                continue
            visible = engine_visible_tools(engine)
            return {
                "visible": [getattr(t, "name", str(t)) for t in visible],
                "maskedCount": engine_masked_count(engine),
            }
        return None

    async def _attachment_add(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        repl._controller.attachments.attach(params["path"])
        return {"attachments": to_jsonable(repl._controller.attachments.list_pending())}

    async def _attachment_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        repl = await self.sessions.get(params["sessionId"])
        repl._controller.attachments.clear()
        return {"attachments": []}

    async def _confirm_respond(self, params: dict[str, Any]) -> dict[str, Any]:
        ok = self.sessions.resolve_confirm(params["requestId"], params)
        return {"ok": ok}

    # ── Voice dictation (engine STT) ──────────────────────────────────────
    async def _stt_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        # Probes plugin + audio runtime; it rarely changes, so it is cached.
        cached = self._stt_status_cache
        if cached is not None and (time.monotonic() - cached[0]) < _STT_STATUS_TTL:
            return cached[1]
        status = self.stt.status()
        self._stt_status_cache = (time.monotonic(), status)
        return status

    async def _stt_start(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.stt.start(
            str(params.get("sessionId") or ""),
            params.get("language"),
        )

    async def _stt_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.stt.stop(str(params.get("sessionId") or ""))

    async def _stt_config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Dictation settings: language/model in the stt plugin section.

        Absent ``modelArch``/``language`` are left untouched; ``null``/``""``
        clears the option (back to the library default — the language goes back to
        following the interface language). It is persisted in ``config.toml`` and
        applied live (``reload``: the next capture rebuilds the engine).
        """
        from phoson_cli.config import save_config

        plugin = self.stt._plugin()
        if plugin is None:
            return {
                "ok": False,
                "reason": "El plugin de dictado (phoson_plugin_stt) no está instalado.",
            }

        values: dict[str, Any] = {}
        if "modelArch" in params:
            raw = params["modelArch"]
            if raw not in (None, ""):
                try:
                    # Validate with the plugin: do not persist an arch that the
                    # engine would later reject when rebuilding.
                    plugin.parse_model_arch(raw)
                except (ValueError, TypeError) as exc:
                    return {"ok": False, "reason": f"model_arch no es válido: {exc}"}
            values["model_arch"] = raw
        if "language" in params:
            raw = params["language"]
            if raw not in (None, ""):
                try:
                    plugin.normalize_language(str(raw))
                except (ValueError, TypeError) as exc:
                    return {"ok": False, "reason": f"language no es válido: {exc}"}
            values["language"] = raw
        if not values:
            return {"ok": True, "status": await self._stt_status({})}

        repl = await self.sessions.get(params["sessionId"])
        merge_plugin_config(repl.config, values)
        path = save_config(repl.config, only_fields={"plugins"})
        # Apply live: discard the engine and cached config so the next capture
        # picks up the new model_arch without restarting the app.
        self.stt.reload()
        self._stt_status_cache = None
        return {"ok": True, "path": str(path), "status": await self._stt_status({})}

    async def _stt_download(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pre-download the dictation model (Settings), with progress.

        The language is the requested one or the one the next capture would
        resolve; progress is emitted as ``stt.progress`` (``phase="downloading"``)
        on the given session.
        """
        engine, reason = self.stt._engine()
        if engine is None:
            return {"ok": False, "reason": reason}
        try:
            language = self.stt.pick_language(params.get("language"))
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        session_key = str(params.get("sessionId") or "")
        loop = asyncio.get_running_loop()

        def _emit_threadsafe(method: str, emitted: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(self._emit, method, emitted)

        result = await asyncio.to_thread(
            self.stt._download_model, language, session_key, _emit_threadsafe
        )
        if result == "unavailable":
            return {
                "ok": False,
                "reason": (
                    "Este runtime de moonshine-voice no expone descarga previa: "
                    "el modelo se baja solo en la primera captura."
                ),
            }
        if result == "failed":
            return {
                "ok": False,
                "reason": (
                    "La descarga del modelo falló (¿sin red?). Reintenta más tarde."
                ),
            }
        return {"ok": True, "result": result, "language": language}

    async def _shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        await self.stt.stop_all()
        await self.sessions.close_all()
        return {"ok": True}


async def run_bridge(protocol_out: TextIO) -> None:
    bridge = Bridge(protocol_out)
    await bridge.serve()


def main(protocol_out: TextIO) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = sys.argv[1:]
    if "--stt-transcribe" in args:
        # Dictate a WAV (no mic or PortAudio): proves that the frozen binary ships
        # the Moonshine runtime and its models, without depending on someone
        # speaking. Usage: phoson-bridge --stt-transcribe audio.wav [language]
        rest = args[args.index("--stt-transcribe") + 1 :]
        wav = rest[0] if rest else ""
        language = rest[1] if len(rest) > 1 else None
        ok, text = stt_transcribe_wav(wav, language)
        payload = {"ok": ok, "text": text}
        protocol_out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        protocol_out.flush()
        sys.exit(0 if ok else 1)
    if "--stt-check" in args:
        # Dictation diagnostics without a mic or protocol loop: prints the status
        # over the protocol channel (real stdout) and exits with code 0/1. Used by
        # scripts/build-sidecar.sh to check that the frozen binary ships the STT
        # engine; it also helps the user debug the microphone button.
        supported, text = stt_report()
        protocol_out.write(text + "\n")
        protocol_out.flush()
        sys.exit(0 if supported else 1)
    try:
        asyncio.run(run_bridge(protocol_out))
    except KeyboardInterrupt:
        pass
