"""UI-independent session runtime.

:class:`SessionController` owns everything that is *not* presentation:
the LLM client, agent engine, tools, plugins, session state (tree,
cursor, metrics), attachments, model/provider switching and the full
run lifecycle (stream consumption, partial persistence, reasoning
capture, session saves).

It presents nothing itself — every user-visible effect goes through the
injected :class:`~phoson_cli.ui_protocols.AgentEventSink`. This is what
lets the front end change (classic ``Renderer``/``ClassicSink`` today,
a full-screen UI going forward) without touching this module — a new
front end is a new sink, not a fork.
"""

import asyncio
import logging
from typing import Any
from pathlib import Path
from dataclasses import dataclass

from phoson_agent import (
    Plugin,
    RunStep,
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentMiddleware,
    AgentStepDoneEvent,
)
from phoson_llm.schemas import (
    REASONING_EFFORTS,
    Message,
    TextBlock,
    ModelConfig,
    ContentBlock,
    ToolDefinition,
)
from phoson_agent.sessions import JsonlStorage, ConversationTree
from phoson_agent.plugins.offload import OffloadMiddleware
from phoson_agent.plugins.summarizer import SummarizationMiddleware
from phoson_agent.plugins.context_window import ContextWindowResolver

from .theme import (
    ThemeRegistry,
    load_theme,
    build_theme_registry,
    default_theme_registry,
)
from .tools import build_tools, build_tools_dict
from .config import COMPACT_MODES, PhosonConfig, build_chat, save_config
from .models import (
    load_models_file,
    provider_settings,
    normalize_provider,
    resolve_context_window,
)
from ._session import SessionState, SessionMetrics
from .commands import build_command_catalog
from .plugin_ui import SinkPluginUiService
from .formatting import ToolRenderRegistry, build_tool_render_registry
from .attachments import AttachmentManager
from .ui_protocols import AgentEventSink, ConfirmationService
from .file_mentions import (
    MAX_MENTIONS_PER_MESSAGE,
    format_file_size,
    expand_file_mentions,
)
from .session_utils import (
    close_plugins,
    build_plugin_specs,
    build_system_prompt,
    drain_monitor_wakes,
    find_monitor_plugin,
)
from .permissions_store import build_permission_middleware

# The monitor plugin ships in the same wheel; the fallback keeps
# source-only dev checkouts (package not installed) importable.
try:
    from phoson_plugin_monitor import render_wake_message
except ImportError:  # pragma: no cover
    render_wake_message = None

#: Upper bound on messages replayed into the chat pane when resuming a
#: session (#56). The full history is shown for any reasonable session;
#: beyond this, resume stays instant and the truncation is announced.
MAX_RESUME_REPLAY_MESSAGES = 200

_LOGGER = logging.getLogger("phoson_cli.controller")


@dataclass
class RunOutcome:
    """Result of a :meth:`SessionController.run_turn`.

    Attributes:
        status: ``"done"``, ``"error"`` or ``"cancelled"``.
        error_code: Error code when status is ``"error"`` (e.g. ``auth``).
        final_content: Final assistant content when status is ``"done"``.
    """

    status: str
    error_code: str | None = None
    final_content: str | None = None


@dataclass
class LoadOutcome:
    """Result of :meth:`SessionController.load_session`."""

    ok: bool
    message: str = ""


@dataclass
class CompactPlan:
    """What a compaction *would* do, before the LLM is consulted (E1).

    Returned by :meth:`SessionController.plan_compaction` so ``/compact``
    can show a preview ("will summarize N of M turns, keeping K") and let
    the user confirm before paying for the summary call.
    """

    ok: bool
    reason: str = ""
    total_messages: int = 0
    summarize_messages: int = 0
    keep_messages: int = 0
    estimated_tokens: int = 0
    profile: str = "balanced"


class SessionController:
    """Owns the session runtime, independent of any UI toolkit.

    Args:
        config: PhosonConfig (provider, model, sessions dir, ...). The
            controller mutates ``config.model`` / ``config.provider`` on
            switch, matching the classic REPL's behavior.
        sink: Presentation target (see ``ui_protocols.AgentEventSink``).
        confirmation: Optional
            ``ui_protocols.ConfirmationService`` for interactive
            yes/no prompts (bash in safe_mode). Injected into the engine
            context as ``bash_confirmation``. Front ends that cannot
            confirm (one-shot) pass nothing and the tool fails closed.
    """

    def __init__(
        self,
        config: PhosonConfig,
        sink: AgentEventSink,
        confirmation: ConfirmationService | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.confirmation = confirmation
        self.storage = JsonlStorage(base_path=config.sessions_dir)
        self._session = SessionState.new()
        self.attachments = AttachmentManager()
        self.current_model = config.model
        self.current_task: asyncio.Task | None = None
        # Serializes user turns with autonomous monitor-wake turns (I-126)
        # so the single-flight engine is never hit by two concurrent runs.
        self._turn_lock = asyncio.Lock()
        # The autonomous wake loop task; started by the front end entry
        # points once an event loop exists (None until then / when the
        # monitor plugin is disabled).
        self._monitor_wake_task: asyncio.Task | None = None
        # Poll interval for the autonomous wake loop; tests shorten it.
        self._wake_poll_seconds = 1.0
        # Per-session plugin command catalog. It is rebuilt with the engine so
        # handlers never retain references to a plugin instance just closed by
        # a provider/model rebuild (I-110).
        self.command_catalog = build_command_catalog(())
        self.tool_render_registry = ToolRenderRegistry({})
        self.theme_registry: ThemeRegistry = default_theme_registry()
        self._command_catalog_version = 0
        # Sub-agent model: explicit override or fallback to main model.
        self.subagent_model: str = config.subagent_model or config.model

        # Context window resolver + token estimator for prompt display.
        self._cw_resolver = ContextWindowResolver(
            ollama_base_url=config.ollama_base_url or "http://localhost:11434",
            openrouter_api_key=config.openrouter_api_key,
            vllm_base_url=self._vllm_base_url(),
        )
        self._context_window: int = 128_000  # default, resolved on first use
        self._context_tokens: int = 0  # current estimated tokens in context

        # Summarization middleware. The provider/model fields are kept in
        # sync with the active config every time ``_rebuild_engine`` runs;
        # the E1 context-management knobs (mode presets) are applied via
        # ``_apply_context_config``.
        self.summarizer = SummarizationMiddleware(
            provider=config.provider,
            model=config.model,
            ollama_base_url=config.ollama_base_url or "http://localhost:11434",
            openrouter_api_key=config.openrouter_api_key,
            vllm_base_url=self._vllm_base_url(),
        )
        # Offload middleware (IMPROVEMENTS.md E1): large tool outputs go
        # to disk and the context keeps head/tail + path.
        self.offload = OffloadMiddleware(
            max_chars=config.offload_max_chars,
            head_chars=config.offload_head_chars,
            tail_chars=config.offload_tail_chars,
            output_dir=config.compacted_dir,
        )
        self._apply_context_config()

        # Per-tool permission gate (IMPROVEMENTS.md A1). ``ask``-level
        # calls route through the front end's confirmation service when
        # one exists; without it the middleware fails closed.
        self.permission_middleware = build_permission_middleware(
            on_ask=self._ask_permission,
        )

        # Build the runtime (chat client, tools, plugins, engine).
        self._rebuild_engine()
        sink.set_session(self._session.tree.session_id)

    # ── Session state ─────────────────────────────────────────────────────

    @property
    def tree(self) -> "ConversationTree":
        """The active conversation tree."""
        return self._session.tree

    @tree.setter
    def tree(self, value: "ConversationTree") -> None:
        self._session.tree = value

    @property
    def current_node_id(self) -> str | None:
        """ID of the most recently active tree node."""
        return self._session.current_node_id

    @current_node_id.setter
    def current_node_id(self, value: str | None) -> None:
        self._session.current_node_id = value

    @property
    def session_metrics(self) -> SessionMetrics:
        """Accumulated metrics for the current session."""
        return self._session.metrics

    @property
    def context_window(self) -> int:
        """Resolved context window for the current model (tokens)."""
        return self._context_window

    @property
    def context_tokens(self) -> int:
        """Estimated tokens currently in context."""
        return self._context_tokens

    @property
    def is_running(self) -> bool:
        """True while an agent run stream is being consumed."""
        return self.current_task is not None and not self.current_task.done()

    # ── Engine (re)construction ───────────────────────────────────────────

    def _build_plugin_specs(self) -> list[str | dict[str, Any] | Plugin]:
        """Community and optional MCP plugin specs for this configuration."""
        return build_plugin_specs(self.config)

    async def _ask_permission(self, tool_name: str, args: dict) -> bool:
        """PermissionMiddleware ask-callback: consult the user.

        Uses the front end's bash-style confirmation for the familiar
        yes/no interaction. Without a confirmation service (one-shot),
        fails closed — the middleware already handles the None case.
        """
        if self.confirmation is None:
            return False
        summary = self._summarize_args_for_confirm(tool_name, args)
        return await self.confirmation.confirm_bash(summary)

    @staticmethod
    def _summarize_args_for_confirm(tool_name: str, args: dict) -> str:
        """One-line human summary of a tool call for the confirm prompt."""
        detail = next((str(v) for v in args.values() if isinstance(v, str) and v), "")
        if len(detail) > 120:
            detail = detail[:117] + "..."
        return f"{tool_name} {detail}".strip()

    def _vllm_base_url(self) -> str | None:
        """Effective vLLM base URL for context-window lookups.

        Mirrors the resolution order used by :func:`build_chat`
        (models.json override, then ``config.vllm_base_url``) so the
        resolver queries the same server the chat client talks to.
        ``None`` lets the resolver fall back to its own default.
        """
        base_url = provider_settings(load_models_file(), "vllm").get("base_url")
        return base_url or self.config.vllm_base_url

    def _apply_context_config(self) -> None:
        """Project the E1 context-management settings onto the middlewares.

        Called from ``__init__`` and from every path that mutates
        provider/model/MCP state (via ``_rebuild_engine``) or the
        compact mode (``/compact on|off|<mode>``):

        - ``compact_mode`` "off" disables *automatic* compaction only —
          manual ``/compact`` keeps working.
        - threshold / min_keep come straight from the config (``load_config``
          already applied the mode presets to values the user left unset).
        - the offload middleware reflects the ``offload_*`` settings and
          ``offload_tool_outputs`` toggles it in or out of the chain.
        """
        self.summarizer.threshold = self.config.compact_threshold
        self.summarizer.min_keep_messages = self.config.compact_min_keep_messages
        self.summarizer.auto_enabled = self.config.compact_mode != "off"

        self.offload.max_chars = self.config.offload_max_chars
        self.offload.head_chars = self.config.offload_head_chars
        self.offload.tail_chars = self.config.offload_tail_chars
        self.offload.output_dir = self.config.compacted_dir

    def _rebuild_engine(self) -> None:
        """(Re)build chat client, tool registry, plugins and the engine.

        Called from ``__init__`` and from every command that mutates
        provider/model/MCP state. The summarizer's provider/model fields
        are also refreshed so token estimation and context-window
        resolution stay accurate.

        The previous runtime (chat client + engine plugins) is closed:
        plugins that expose ``aclose`` (e.g. the MCP plugin with its
        pooled sessions) are closed asynchronously on the running loop;
        without a loop the synchronous ``cleanup()`` fallback is used.
        """
        old_chat = getattr(self, "chat", None)
        old_engine = getattr(self, "engine", None)
        old_plugins = (
            list(getattr(old_engine, "_loaded_plugins", [])) if old_engine else []
        )
        self.chat = build_chat(self.config)
        # Release the old client's connection pool (e.g. Anthropic SDK
        # holds a persistent httpx.AsyncClient). Schedule on the running
        # loop; no-op on the first call from __init__.
        if old_chat is not None and hasattr(old_chat, "aclose"):
            try:
                asyncio.get_running_loop().create_task(old_chat.aclose())
            except RuntimeError:
                pass
        # Release the old engine's plugin resources (e.g. MCP pooled
        # sessions / STDIO subprocesses) so switching model/provider does
        # not leak them. Failures are logged, never fatal to the rebuild.
        if old_plugins:
            try:
                asyncio.get_running_loop().create_task(close_plugins(old_plugins))
            except RuntimeError:
                for plugin in old_plugins:
                    try:
                        plugin.cleanup()
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning(
                            "Could not clean up plugin %r during engine rebuild",
                            getattr(plugin, "name", "?"),
                            exc_info=True,
                        )
        self.tools = build_tools()
        self.tools_dict = build_tools_dict()

        self.summarizer.provider = self.config.provider
        self.summarizer.model = self.config.model
        self.summarizer.vllm_base_url = self._vllm_base_url()
        self._apply_context_config()

        plugins = self._build_plugin_specs()

        # Middleware order matters: offload rewrites tool results first
        # (so oversized outputs never reach the summarizer's context
        # accounting), then the summarizer may compact, then the
        # permission gate intercepts tool calls. Offload only joins the
        # chain when ``offload_tool_outputs`` is enabled (E1).
        middlewares: list[AgentMiddleware] = []
        if self.config.offload_tool_outputs:
            middlewares.append(self.offload)
        middlewares.extend([self.summarizer, self.permission_middleware])

        self.engine = AgentEngine(
            chat=self.chat,
            tools=self.tools,
            middlewares=middlewares,
            plugins=plugins,
            max_iterations=self.config.max_iterations,
        )
        self._command_catalog_version += 1
        loaded_plugins = getattr(self.engine, "_loaded_plugins", [])
        try:
            self.command_catalog = build_command_catalog(
                loaded_plugins, version=self._command_catalog_version
            )
            self.tool_render_registry = build_tool_render_registry(
                loaded_plugins, [tool.name for tool in self.engine.tools]
            )
            self.theme_registry = build_theme_registry(loaded_plugins)
        except Exception:
            # AgentEngine has already initialized these plugins. Extension
            # validation must not leak a pool/process/task just because a
            # command, theme, or render spec is invalid during bootstrap.
            for plugin in loaded_plugins:
                try:
                    plugin.cleanup()
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Could not clean up plugin %r after extension "
                        "validation failure",
                        getattr(plugin, "name", "?"),
                        exc_info=True,
                    )
            raise
        set_tool_render_registry = getattr(self.sink, "set_tool_render_registry", None)
        if set_tool_render_registry is not None:
            set_tool_render_registry(self.tool_render_registry)

        # Inject runtime context for sub-agents.
        self.engine.context.extra["safe_mode"] = self.config.safe_mode
        self.plugin_ui = SinkPluginUiService(
            self.sink,
            load_theme(self.config.theme, registry=self.theme_registry),
            confirmation=self.confirmation,
        )
        self.engine.context.extra["plugin_ui"] = self.plugin_ui

        # Mirror the engine's tool schemas (built-in + plugin/MCP tools)
        # into the summarizer so the auto-compact gate counts the schema
        # weight of every request (IMPROVEMENTS.md I-91).
        self.summarizer.tool_definitions = [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.engine.tools
        ]
        self.engine.context.extra["available_tools"] = self.tools_dict
        self.engine.context.extra["default_model"] = self.subagent_model
        # Fallback for sub-agents when the subagent model is unavailable.
        self.engine.context.extra["main_model"] = self.current_model
        self.engine.context.extra["max_iterations"] = self.config.max_iterations
        self.engine.context.extra["subagent_max_parallel"] = (
            self.config.subagent_max_parallel
        )
        self.engine.context.extra["subagent_timeout_seconds"] = (
            self.config.subagent_timeout_seconds
        )
        self.engine.context.extra["chat"] = self.chat
        # Live sub-agent metrics (E2): the tools create a fresh tracker
        # per call and push it to the front end through this callback
        # (bound to ``sink.on_subagent_progress``).
        self.engine.context.extra["on_subagent_progress"] = (
            self.sink.on_subagent_progress
        )
        # Interactive confirmations (safe_mode bash). None → the tool
        # fails closed (one-shot / non-interactive front ends).
        self.engine.context.extra["bash_confirmation"] = self.confirmation

        # Monitor plugin (I-126): inject a *provider* (callable), not a
        # snapshot — new_session()/load_session() swap the tree without
        # rebuilding the engine, so a static value would go stale. The
        # register_monitor tool injects it via @tool(inject=...) and
        # calls it at registration time.
        self.engine.context.extra["session_id_provider"] = lambda: (
            self._session.tree.session_id
        )

        # (Re)start any running monitors from disk: engine rebuilds
        # (/model, /provider, /mcp) kill the previous instance's tasks,
        # and a crash leaves them running on disk. Duck-typed so this
        # stays a no-op when the plugin is not enabled.
        monitor_plugin = find_monitor_plugin(
            list(getattr(self.engine, "_loaded_plugins", []))
        )
        if monitor_plugin is not None:
            ensure = getattr(monitor_plugin, "ensure_started", None)
            if ensure is not None:
                try:
                    asyncio.get_running_loop().create_task(
                        ensure(),
                        name="monitors:ensure_started",
                    )
                except RuntimeError:
                    # No running loop (sync test/one-shot bootstrap): the
                    # tools start tasks lazily on first use.
                    pass

    async def shutdown(self) -> None:
        """Release the chat client and any loaded engine plugins.

        Idempotent. Called by front ends on exit (classic REPL on EOF,
        front end on shutdown) so no HTTP pools or MCP subprocesses
        outlive the session.
        """
        # Stop the autonomous monitor wake loop first: it must not start
        # a wake turn after the front end is gone. Monitors stay
        # registered on disk and the next host resurrects them (I-126).
        if self._monitor_wake_task is not None and not self._monitor_wake_task.done():
            self._monitor_wake_task.cancel()
            try:
                await self._monitor_wake_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._monitor_wake_task = None
        plugins = list(getattr(self.engine, "_loaded_plugins", []))
        if plugins:
            await close_plugins(plugins)
        aclose = getattr(self.chat, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Could not close chat client on shutdown")

    # ── Run lifecycle ─────────────────────────────────────────────────────

    def _build_user_message(self, user_input: str) -> Message:
        """Flush pending attachments, expand ``@file`` mentions, build the Message.

        The message the model receives is the user's raw text (so the
        ``@mention`` tokens stay visible in context) followed by the blocks
        each mention resolved to — inlined text for code/data files, native
        media blocks for images/audio/video/pdf (same blocks ``/attach``
        builds). Mentions that do not resolve are left as literal text and
        reported to the user, never silently dropped.
        """
        pending_blocks: list[ContentBlock] = []
        if self.attachments:
            media_blocks = list(self.attachments.flush())
            self.sink.on_attachments(
                [block.source.split("file://", 1)[-1] for block in media_blocks]
            )
            pending_blocks = list(media_blocks)

        mention_blocks: list[ContentBlock] = []
        if user_input:
            pending_blocks.insert(0, _text_block(user_input))
            expanded = expand_file_mentions(user_input, cwd=Path.cwd())
            mention_blocks = list(expanded.blocks)
            attached = [
                f"{m.raw[1:]} ({format_file_size(m.path.stat().st_size)})"
                for m in expanded.mentions
                if m.ok
            ]
            if attached:
                self.sink.notify("info", "Attached: " + ", ".join(attached))
            for m in expanded.mentions:
                if not m.ok:
                    self.sink.notify("warn", f"@{m.raw[1:]}: {m.error}")
            if expanded.truncated:
                self.sink.notify(
                    "warn",
                    f"Only the first {MAX_MENTIONS_PER_MESSAGE} @mentions were "
                    "attached.",
                )

        content: str | list[ContentBlock] = (
            pending_blocks + mention_blocks
            if (pending_blocks or mention_blocks)
            else user_input
        )
        return Message(role="user", content=content)

    def _append_user_turn(self, message: Message) -> tuple[str, list[Message]]:
        """Append user message to the tree.

        Returns:
            Tuple of (new_node_id, full conversation path).
        """
        node = self.tree.append(
            parent_id=self.current_node_id,
            message=message,
        )
        self.current_node_id = node.id
        path = self.tree.get_path(self.current_node_id)
        return node.id, path

    def cancel_current(self) -> bool:
        """Cancel the in-flight run, if any. Returns True if one was cancelled."""
        task = self.current_task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def _consume_stream(
        self, path: list[Message], config: ModelConfig
    ) -> AgentDoneEvent | AgentErrorEvent:
        """Run the agent stream loop, forwarding events to the sink.

        Returns the terminal event — ``AgentDoneEvent`` on success or
        ``AgentErrorEvent`` when the run failed (auth errors, tool
        failures, max iterations). The engine's contract is that exactly
        one terminal event is emitted; a stream that ends without either
        is a protocol bug and raises ``RuntimeError``.

        Re-raises ``asyncio.CancelledError`` without catching — the caller
        (``run_turn``) handles it so ``base_count`` stays in scope for the
        partial save.
        """
        terminal: AgentDoneEvent | AgentErrorEvent | None = None

        async def consume() -> None:
            nonlocal terminal
            async for event in self.engine.stream(path, config):
                # Live metrics (I-88): fold each completed step into the
                # session totals and refresh the context indicator as the
                # run progresses, not only at the end. The front end's
                # header reads these, so cost and tokens track the run.
                if isinstance(event, AgentStepDoneEvent):
                    self._update_live_metrics(event.step, config)
                self.sink.on_event(event)
                if isinstance(event, (AgentDoneEvent, AgentErrorEvent)):
                    terminal = event

        self.current_task = asyncio.create_task(consume())
        try:
            await self.current_task
        finally:
            self.current_task = None

        if terminal is None:
            raise RuntimeError(
                "Agent stream ended without a terminal AgentDoneEvent/AgentErrorEvent"
            )
        return terminal

    def _update_live_metrics(self, step: "RunStep", config: ModelConfig) -> None:
        """Fold one completed step into the live session metrics (I-88).

        Called from :meth:`_consume_stream` as each
        :class:`AgentStepDoneEvent` arrives, so the header's cost and
        token indicators track the run in real time. Cost/token totals
        are accumulated here (once, per step) — :meth:`_finalize_run`
        must NOT re-add them, or every step would be counted twice.

        The context indicator is refreshed against the engine's
        in-flight history (the same conservative estimate the auto-
        compact gate uses), not the tree — the tree is only updated at
        the end of the run.
        """
        self.session_metrics.add_run_step(step)
        self._context_tokens = self._estimate_in_flight(config)

    def _estimate_in_flight(self, config: ModelConfig) -> int:
        """Conservative token estimate of the engine's in-flight history.

        Mirrors :meth:`estimate_active_path` but reads the engine's live
        history (which includes the current run's messages before they
        land in the tree) so the header tracks the run as it happens.
        """
        history = self.engine.get_partial_history()
        return self.summarizer.estimate_request(
            history,
            system=config.system,
            tools=self.summarizer.tool_definitions,
        )

    def _finalize_run(self, done_event: AgentDoneEvent, base_count: int) -> None:
        """Append new messages to tree and refresh the context indicator.

        Session metrics (cost/tokens/steps) are already accumulated live
        in :meth:`_update_live_metrics` as each step completes (I-88) —
        re-adding ``done_event.result.steps`` here would double-count
        every step, so this only updates the tree and the final context
        indicator (from the now-committed tree path).
        """
        if self._rebase_after_compaction(done_event.result.history):
            return

        new_messages = done_event.result.history[base_count:]
        if new_messages:
            created = self.tree.append_many(self.current_node_id, new_messages)
            self.current_node_id = created[-1].id

        self._context_tokens = self.estimate_active_path()

    def _append_partial_history(self, base_count: int) -> None:
        """Slice engine partial history and append new nodes to the tree.

        Updates ``current_node_id`` to the last appended node.
        """
        partial = self.engine.get_partial_history()
        if self._rebase_after_compaction(partial):
            return
        new_messages = partial[base_count:]
        if new_messages:
            created = self.tree.append_many(self.current_node_id, new_messages)
            self.current_node_id = created[-1].id

    def _rebase_after_compaction(self, history: list[Message]) -> bool:
        """Rebase the tree onto a compacted engine history (I-91).

        When a mid-run auto-compaction (or the emergency 400 rescue)
        spliced the engine's history, the tree's active path no longer
        matches it — appending the tail would duplicate the compacted
        messages. The fix mirrors the manual ``/compact``: graft the
        compacted history as a **new branch off the root** (the old
        branch stays intact, visible via ``/tree``) and move the cursor
        to its last node.

        Returns:
            True when a rebase happened (the caller must not also append
            the history tail).
        """
        compact_events = self.summarizer.pop_compact_events()
        if not compact_events:
            return False
        if not history:
            return False
        created = self.tree.append_many(None, history)
        self.current_node_id = created[-1].id
        self._context_tokens = self.estimate_active_path()
        self.sink.notify(
            "info",
            f"Context auto-compacted ({compact_events[0].original_tokens} → "
            f"{compact_events[0].compacted_tokens} tokens, "
            f"{compact_events[0].messages_removed} messages summarized).",
        )
        return True

    def estimate_active_path(self) -> int:
        """Conservative token estimate of the active path (I-91).

        Counts messages + system prompt + tool schemas — the same number
        the auto-compact gate uses, so the header indicator never lags
        behind the gate.
        """
        path = self.tree.get_path(self.current_node_id)
        return self.summarizer.estimate_request(
            path,
            system=build_system_prompt(self.engine.tools),
            tools=self.summarizer.tool_definitions,
        )

    def _persist_run_reasoning(self) -> None:
        """Attach captured reasoning to the last assistant node on the path.

        Stored in ``node.metadata["reasoning"]`` (a generic, backward
        compatible dict) so it survives session resume and can be
        expanded later (Ctrl+T in the classic REPL, a collapsible in
        the TUI).
        """
        reasoning = self.sink.take_reasoning()
        if not reasoning or self.current_node_id is None:
            return
        node = self.tree.nodes.get(self.current_node_id)
        if node is not None and node.message.role == "assistant":
            node.metadata["reasoning"] = reasoning

    async def _save_session(self) -> None:
        self._ensure_session_title()
        await self.storage.save(self.tree)
        await self.storage.save_meta(
            self.tree.session_id, self.session_metrics.to_meta()
        )

    def _ensure_session_title(self) -> None:
        """Auto-generate a session title from the first user message (#55).

        Only fires once, and only when the session has no title yet (the
        user's explicit ``/title`` always wins). The heuristic is cheap
        on purpose — first line of the first user message, truncated to
        60 chars — so no extra LLM round trip is spent on naming.
        """
        if self.tree.title:
            return
        for node in self.tree.nodes.values():
            msg = node.message
            if msg.role != "user":
                continue
            content = msg.content
            text = (
                content
                if isinstance(content, str)
                else " ".join(
                    b.text for b in content if isinstance(b, TextBlock) and b.text
                )
            ).strip()
            if not text:
                continue
            # Skip command-ish inputs; they make poor titles.
            if text.startswith("/"):
                return
            first_line = text.splitlines()[0].strip()
            title = first_line[:57] + "…" if len(first_line) > 60 else first_line
            self.tree.title = title or None
            return

    async def run_turn(self, user_input: str) -> RunOutcome:
        """Execute one agent turn (run, persist, notify the sink).

        All user-visible effects go through the sink; the tree, metrics
        and session storage are updated in every terminal state:

        - done:      new messages appended, reasoning persisted, saved.
        - error:     partial history + reasoning persisted, saved, and
                     an auth error produces an actionable warning.
        - cancelled: same as error (partial progress saved).

        Serialized against autonomous monitor-wake turns via
        ``_turn_lock`` (the engine is single-flight).
        """
        async with self._turn_lock:
            # Monitor plugin (I-126): fold any wakes that fired while the
            # user was composing into THIS message (the autonomous wake loop
            # only fires turns while idle, so at this point any pending wake
            # belongs to the user's current turn).
            monitor_plugin = find_monitor_plugin(
                list(getattr(self.engine, "_loaded_plugins", []))
            )
            wake_events = await drain_monitor_wakes(
                monitor_plugin, self._session.tree.session_id
            )
            if wake_events:
                self.sink.notify(
                    "info",
                    f"{len(wake_events)} monitor wake(s) delivered with your message.",
                )
            return await self._execute_turn(user_input, wake_events, "user")

    async def _execute_turn(
        self,
        user_input: str,
        wake_events: list[Any] | None = None,
        source: str = "user",
    ) -> RunOutcome:
        """Run one turn built from ``user_input`` (+ optional wake header).

        ``source`` only names the trigger (``user`` or ``monitor``); the
        sink sees both through the same ``on_user_message`` channel so the
        front ends render autonomous wake turns exactly like typed ones.
        """
        effective_input = user_input
        if wake_events:
            if render_wake_message is not None:
                header = render_wake_message(wake_events)
            else:  # pragma: no cover — package always ships in the wheel
                header = "[MONITOR EVENTS] " + "; ".join(
                    f"{e.monitor} ({e.kind})" for e in wake_events
                )
            effective_input = header + "\n\n" + user_input

        user_message = self._build_user_message(effective_input)
        _node_id, path = self._append_user_turn(user_message)
        base_count = len(path)

        # Retained reasoning (IMPROVEMENTS.md E1): register the current
        # path's captured reasoning so an auto-compaction mid-run can fold
        # it into the summary (chain of thought, not just conclusions).
        # Cleared when the run ends, whatever the terminal state.
        self.summarizer.set_retained_reasoning(path, self._path_reasoning_map(path))

        # Drop compaction events queued by a *previous* turn or a manual
        # /compact — only compactions that happen during THIS run should
        # trigger the tree rebase in _finalize_run (I-91).
        self.summarizer.pop_compact_events()

        self.sink.on_user_message(effective_input, user_message)

        reasoning_effort = self.config.reasoning_effort
        if reasoning_effort not in REASONING_EFFORTS:
            reasoning_effort = None
        config = ModelConfig(
            model=self.current_model,
            system=build_system_prompt(self.engine.tools),
            reasoning_effort=reasoning_effort,
            # Stable per-conversation key: OpenRouter uses it for sticky
            # routing so the upstream prompt cache stays warm (G2 / #69).
            session_id=self._session.tree.session_id,
        )

        await self._refresh_context_window()
        self._context_tokens = self.estimate_active_path()

        try:
            terminal_event = await self._consume_stream(path, config)
        except asyncio.CancelledError:
            self.summarizer.clear_retained_reasoning()
            self.sink.flush_line()
            self.sink.capture_partial_reasoning()
            self._append_partial_history(base_count)
            self._persist_run_reasoning()
            await self._save_session()
            self.sink.notify("warn", "Partial progress saved.")
            return RunOutcome(status="cancelled")

        if isinstance(terminal_event, AgentErrorEvent):
            self.summarizer.clear_retained_reasoning()
            # The sink already showed the error panel. Persist what exists
            # (the user turn, plus any steps that succeeded before the
            # failure) so the conversation is not lost and can be retried
            # or /undo'd.
            self._append_partial_history(base_count)
            self._persist_run_reasoning()
            await self._save_session()
            if terminal_event.code == "auth":
                self.sink.notify(
                    "warn",
                    "Check your credentials — run /setup or set the "
                    "provider API key env var.",
                )
            return RunOutcome(status="error", error_code=terminal_event.code)

        self._finalize_run(terminal_event, base_count)
        self._persist_run_reasoning()
        self.summarizer.clear_retained_reasoning()
        await self._save_session()
        return RunOutcome(
            status="done",
            final_content=terminal_event.result.final_content,
        )

    # ── Autonomous monitor wake (I-126) ──────────────────────────────────

    def start_monitor_wake_loop(self) -> None:
        """Start the autonomous wake loop (no-op when disabled/already up).

        The loop is what *reactivates the agent while idle*: when a
        monitor fires and no run is in flight, the pending wakes trigger
        a turn of their own — no user message required. Front end entry
        points call this once the event loop is running (the controller's
        ``__init__`` runs before the loop in the classic REPL, where
        tasks cannot be created yet).
        """
        if not self.config.enable_monitors:
            return
        if self._monitor_wake_task is not None and not self._monitor_wake_task.done():
            return
        self._monitor_wake_task = asyncio.create_task(
            self._monitor_wake_loop(),
            name="monitors:wake-loop",
        )

    def _peek_pending_wakes(self, plugin: Plugin) -> list[Any]:
        """Non-destructive peek at pending wakes for the current session."""
        peek = getattr(plugin, "pending_wakes", None)
        if peek is None:
            return []  # plugin build without the non-destructive view
        try:
            return list(peek(self._session.tree.session_id) or [])
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not peek monitor wakes", exc_info=True)
            return []

    async def _monitor_wake_loop(self) -> None:
        """Re-activate the agent from pending wakes while it is idle.

        - While a run is in flight (``is_running``) or a user turn holds
          ``_turn_lock``, the loop does nothing: wakes that arrive then
          are drained by the user turn itself (``run_turn``).
        - Otherwise each wake batch becomes an autonomous turn whose user
          message is the ``[MONITOR EVENTS]`` header, so the front ends
          render it like any typed turn.
        - The drain happens *inside* the turn lock: if a user turn ran in
          the meantime it already consumed the wakes and this tick is a
          no-op. The loop is cancelled on shutdown.
        """
        while True:
            try:
                await self._wake_loop_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a broken tick must never
                # kill the loop: the next tick re-evaluates.
                _LOGGER.warning("Monitor wake loop tick failed", exc_info=True)
            await asyncio.sleep(self._wake_poll_seconds)

    async def _wake_loop_tick(self) -> None:
        """One poll of the autonomous wake loop (see the loop's docstring)."""
        plugin = find_monitor_plugin(list(getattr(self.engine, "_loaded_plugins", [])))
        if plugin is None or self.is_running:
            return
        if not self._peek_pending_wakes(plugin):
            return
        self.sink.notify(
            "info",
            "Monitor wake(s) received — waking the agent.",
        )
        async with self._turn_lock:
            wake_events = await drain_monitor_wakes(
                plugin, self._session.tree.session_id
            )
            if not wake_events:
                return  # a user turn consumed them in the meantime
            try:
                await self._execute_turn("", wake_events, source="monitor")
            except Exception:  # noqa: BLE001 — a broken wake turn
                _LOGGER.warning("Autonomous monitor wake turn failed", exc_info=True)

    def monitor_status(self) -> str | None:
        """Short status string for active monitors, or ``None`` when none.

        Thin host-side accessor: duck-typed on the monitor plugin's
        optional ``monitor_status()`` hook so the front ends can surface
        "monitors are running" in a header/prompt without importing the
        package. In-memory only; safe to call on every paint.
        """
        plugin = find_monitor_plugin(list(getattr(self.engine, "_loaded_plugins", [])))
        if plugin is None:
            return None
        status_fn = getattr(plugin, "monitor_status", None)
        if status_fn is None:
            return None
        try:
            return status_fn()
        except Exception:  # noqa: BLE001 — a status probe must never
            # break a paint; degrade to "no indicator".
            _LOGGER.warning("monitor_status() failed", exc_info=True)
            return None

    # ── Session / model management ────────────────────────────────────────

    def build_system_prompt(self) -> str:
        """System prompt for the next run (built-in + loaded MCP tools)."""
        return build_system_prompt(self.engine.tools)

    def new_session(self) -> None:
        """Start a fresh session, resetting tree and metrics."""
        self._session.reset()
        self.attachments.clear()
        self.sink.set_session(self._session.tree.session_id)

    # ── Manual compaction (IMPROVEMENTS.md C2 + E1) ────────────────────

    def set_compact_mode(self, mode: str) -> bool:
        """Set the automatic-compaction mode (E1: ``/compact on|off|<mode>``).

        Args:
            mode: ``"on"`` (re-enable with the configured mode), ``"off"``
                (disable automatic compaction; manual /compact still
                works), ``"balanced"`` or ``"aggressive"`` (re-enable with
                that mode, applying its preset).

        Returns:
            True when the mode was applied and persisted; False on an
            unknown value (the message is reported via the sink).
        """
        if mode not in COMPACT_MODES and mode != "on":
            self.sink.notify(
                "error",
                f"Unknown compact mode {mode!r} — use balanced, aggressive, on or off.",
            )
            return False

        if mode == "on":
            # Re-enable with the previously configured mode when it is a
            # real mode; "off" (or anything unset) falls back to balanced.
            current = self.config.compact_mode
            mode = current if current in ("balanced", "aggressive") else "balanced"

        self.config.compact_mode = mode
        self._apply_context_config()
        try:
            save_config(self.config, only_fields={"compact_mode"})
        except OSError as exc:  # pragma: no cover - best-effort persist
            _LOGGER.warning("Could not persist compact_mode: %s", exc)
        self.sink.notify(
            "info",
            f"Auto-compaction {'disabled' if mode == 'off' else f'enabled ({mode})'}."
            " Manual /compact still works.",
        )
        return True

    def _profile_keep(self, profile: str | None) -> int:
        """Messages kept verbatim for a compaction *profile* (E1).

        ``balanced`` keeps the configured tail; ``aggressive`` halves it
        (floor of 1) so the compaction cuts deeper.
        """
        base = max(2, self.config.compact_min_keep_messages)
        if profile == "aggressive":
            return max(1, base // 2)
        return base

    def _path_reasoning_map(self, path: list[Message]) -> dict[int, str]:
        """Captured reasoning per path position (retained reasoning, E1).

        Reasoning is persisted on ``node.metadata["reasoning"]`` by
        :meth:`_persist_run_reasoning`; the tree's Message objects are the
        same instances the engine history uses, so identity matching is
        stable.

        Note: after a compaction the tree can hold two nodes referencing
        the *same* Message object (the old branch's node — with reasoning
        in its metadata — and the new branch's node, which reuses the
        object but has empty metadata). A non-empty reasoning therefore
        wins over an empty one for a given identity, so a re-branch never
        wipes the retained reasoning.
        """
        reasoning_by_msg: dict[int, str] = {}
        for node in self.tree.nodes.values():
            text = node.metadata.get("reasoning", "")
            if text:
                # First non-empty wins; an empty later node must not clobber it.
                reasoning_by_msg.setdefault(id(node.message), text)
        return {
            idx: text
            for idx, msg in enumerate(path)
            if (text := reasoning_by_msg.get(id(msg), ""))
        }

    def plan_compaction(self, profile: str | None = None) -> "CompactPlan":
        """Compute what a compaction *would* do, without any LLM call.

        Powers the /compact preview (E1: "preview before applying").
        """
        path = self.tree.get_path(self.current_node_id)
        if not path:
            return CompactPlan(ok=False, reason="The session is empty.")

        others = [m for m in path if m.role != "system"]
        min_keep = self._profile_keep(profile)
        if len(others) <= min_keep:
            return CompactPlan(
                ok=False,
                reason=(
                    f"Only {len(others)} turn(s) in context — nothing worth compacting."
                ),
            )

        summarize = len(others) - min_keep
        estimated = self.summarizer.estimate_tokens(others[:summarize])
        return CompactPlan(
            ok=True,
            total_messages=len(others),
            summarize_messages=summarize,
            keep_messages=min_keep,
            estimated_tokens=estimated,
            profile=profile or "balanced",
        )

    async def compact_context(
        self, profile: str | None = None
    ) -> tuple[int, int, bool]:
        """Manually compact the conversation (IMPROVEMENTS.md C2 + E1).

        Asks the LLM for a **structured handoff summary** of the current
        path, then rewrites the conversation as a **new branch** off the
        root: summary message first, followed by the profile's recent-tail
        messages. The old branch stays intact in the tree (visible via
        ``/tree``), so the compaction is reversible by inspection and the
        session keeps its identity.

        E1 upgrades over C2:
        - the summary is a structured document (Goal/Completed/Decisions/
          Reasoning highlights/Next steps/Constraints) instead of free
          text, so the next segment consumes it reliably;
        - captured reasoning from previous turns (``node.metadata``) is
          folded into the summary — retained reasoning, not just content;
        - ``profile="aggressive"`` keeps a shorter tail.

        Returns:
            ``(before_tokens, after_tokens, ok)`` — token estimates before
            and after (equal when there was nothing to compact) and whether
            the path actually changed.
        """
        path = self.tree.get_path(self.current_node_id)
        if not path:
            self.sink.notify("info", "Nothing to compact yet — the session is empty.")
            return 0, 0, False

        before = self.summarizer.estimate_tokens(path)
        system_msgs = [m for m in path if m.role == "system"]
        others = [m for m in path if m.role != "system"]
        min_keep = self._profile_keep(profile)
        if len(others) <= min_keep:
            self.sink.notify(
                "info",
                f"Only {len(others)} turn(s) in context — nothing worth compacting.",
            )
            return before, before, False

        # One plain LLM round trip for the structured summary. The chat
        # client is used directly (no tools, no engine loop); errors
        # propagate to the caller so /compact can report them without
        # touching the session. Retained reasoning from this path is
        # folded into the prompt (E1).
        from phoson_llm.schemas import LLMDoneEvent

        history_msgs = others[:-min_keep]
        # Reasoning must be indexed against *history_msgs* (the exact list
        # handed to the prompt builder), not the full path — otherwise a
        # leading system message would shift every index.
        reasoning = self._path_reasoning_map(history_msgs)
        summary_prompt = self.summarizer.build_summary_prompt(
            history_msgs, reasoning_for=reasoning
        )
        done: LLMDoneEvent = await self.chat.complete(
            [Message(role="user", content=summary_prompt)],
            ModelConfig(model=self.current_model, max_tokens=4096, temperature=0.3),
        )

        summary_text = done.content.strip()
        if not summary_text:
            self.sink.notify(
                "warn", "The model returned an empty summary — no changes."
            )
            return before, before, False

        compacted_msgs = list(system_msgs)
        compacted_msgs.append(
            Message(role="user", content=f"[Conversation summary]: {summary_text}")
        )
        keep_msgs = others[-min_keep:]
        compacted_msgs.extend(keep_msgs)

        after = self.summarizer.estimate_tokens(compacted_msgs)
        self.tree.append_many(None, compacted_msgs)
        leaves = self.tree.get_leaves()
        latest = max(leaves, key=lambda nid: self.tree.nodes[nid].created_at)
        self.current_node_id = latest

        # Record the event so telemetry sees manual compactions too.
        self.summarizer.record_compaction_event(
            original_tokens=before,
            compacted_tokens=after,
            messages_removed=len(path) - len(compacted_msgs),
            summary_length=len(summary_text),
        )
        return before, after, True

    async def load_session(self, session_id: str) -> LoadOutcome:
        """Load a session from storage and replay its tail."""
        try:
            self._session.tree = await self.storage.load(session_id)
            self._session.current_node_id = self.find_latest_node_id()
            self._session.metrics = SessionMetrics()
            self.sink.set_session(self._session.tree.session_id)

            # Load saved metrics using the authoritative SessionMeta field names.
            metas = await self.storage.list_meta()
            for meta in metas:
                if str(meta.id) == session_id:
                    self.session_metrics.total_cost_usd = meta.total_cost
                    self.session_metrics.total_output_tokens = meta.total_tokens
                    self.session_metrics.step_count = meta.step_count
                    self.session_metrics.last_model = meta.last_model or ""
                    break

            # Replay the session so the user knows where they left off.
            # The full path is rendered (#56) — the chat pane can only
            # scroll through what's in sink.blocks, so a fixed tail made
            # older messages unreachable after resuming. Very long paths
            # are capped to keep resume instant; a notice states how
            # much was truncated (see render_history's tail rule).
            try:
                path = self.tree.get_path(self.current_node_id)
                if len(path) > MAX_RESUME_REPLAY_MESSAGES:
                    self.sink.print_history(path, tail=MAX_RESUME_REPLAY_MESSAGES)
                else:
                    self.sink.print_history(path)
            except (ValueError, AttributeError, TypeError):
                _LOGGER.debug(
                    "Could not replay session history — node may be corrupted",
                    exc_info=True,
                )

            return LoadOutcome(ok=True)
        except FileNotFoundError:
            message = f"Session {session_id[:8]} not found."
            self.sink.notify("error", message)
            return LoadOutcome(ok=False, message=message)
        except Exception as e:  # noqa: BLE001
            _LOGGER.exception("Failed to load session %s", session_id[:8])
            message = f"Failed to load session: {e}"
            self.sink.notify("error", message)
            return LoadOutcome(ok=False, message=message)

    async def _refresh_context_window(self) -> None:
        """Resolve and cache the context window for ``current_model``.

        Same resolution order as ``run_turn``: a ``models.json`` override
        wins, otherwise the live registry is queried. Also called from
        ``set_model``/``set_provider`` so the header's indicator reflects
        the new model immediately, not just after the next turn.
        """
        models_data = load_models_file()
        model_bare = self.current_model.split("/", 1)[-1]
        override = resolve_context_window(models_data, self.config.provider, model_bare)
        if override is not None:
            self._context_window = override
        else:
            self._context_window = await self._cw_resolver.resolve(
                self.config.provider, self.current_model
            )

    async def set_provider(self, provider: str) -> None:
        """Switch to a different provider and rebuild runtime state.

        If ``models.json`` defines a ``default_model`` for the new
        provider, that model is selected; otherwise the current model
        name is kept.
        """
        self.config.provider = provider
        settings = provider_settings(load_models_file(), provider)
        default_model = settings.get("default_model")
        await self.set_model(default_model or self.config.model)

    async def set_model(self, model: str, provider: str | None = None) -> None:
        """Switch to a different model, rebuild the engine, refresh context window.

        When ``provider`` is given and differs from the active one, the
        provider is switched too (I-89): a model id that belongs to another
        provider must leave the runtime — and the persisted config — as a
        consistent ``(provider, model)`` pair.
        """
        if provider is not None and normalize_provider(provider) != normalize_provider(
            self.config.provider
        ):
            self.config.provider = provider
        self.current_model = model
        self.config.model = model
        # Sub-agent model follows the main model unless explicitly overridden.
        self.subagent_model = self.config.subagent_model or model
        self._rebuild_engine()
        await self._refresh_context_window()

    def label_current_node(self, text: str) -> None:
        """Label the current node with text."""
        if self.current_node_id is None:
            return
        self.tree.label(self.current_node_id, text)

    def undo_last_turn(self) -> tuple[bool, str]:
        """Move the cursor back to just before the last user turn.

        The "undone" messages are not deleted — they remain in the tree
        (visible via /tree) as an abandoned branch; the next user message
        appends a new branch from the restored cursor position. Session
        cost/token metrics are cumulative and are intentionally NOT
        rolled back.

        Returns:
            Tuple of (success, message or new node id).
        """
        if self.current_node_id is None:
            return False, "No active node — nothing to undo."

        node_path = self._node_path()
        last_user_idx = next(
            (
                i
                for i in range(len(node_path) - 1, -1, -1)
                if node_path[i].message.role == "user"
            ),
            None,
        )
        if last_user_idx is None:
            return False, "No user turn found in the current path."
        if last_user_idx == 0:
            return False, "Nothing to undo — the session starts with this turn."

        self.current_node_id = node_path[last_user_idx - 1].id
        return True, self.current_node_id

    def _node_path(self) -> list:
        """The active path root → cursor as node objects (empty when idle tree)."""
        node_path: list = []
        cursor: str | None = self.current_node_id
        while cursor is not None:
            node = self.tree.nodes[cursor]
            node_path.append(node)
            cursor = node.parent_id
        node_path.reverse()
        return node_path

    def jump_candidates(self) -> list[tuple[str, str]]:
        """Rewind targets: ``(user_node_id, preview)`` pairs, newest first (G1).

        One entry per *genuine user* node on the active path whose parent
        exists — picking one lands the cursor on that node's *parent*,
        i.e. right before the selected turn, so the next user message
        replaces it and everything after (Claude Code's double-Esc UX).
        The first root node is skipped: there is no earlier node to land
        on. The preview is the message's plain text truncated to one line.

        Ordering and filtering (issue #109):
        - **Newest → oldest.** The path is walked in reverse, so the
          candidate list (and the picker's initial cursor) starts at the
          most recent user turn — the most likely rewind target.
        - **Content-aware filter.** Tool results are stored in the tree
          with role ``user`` (``_tool_runner`` appends
          ``Message(role="user", content=[ToolResultBlock(...)])``), so
          a role-only check leaks them in as "(empty message)" rows.
          A node qualifies only if its content is a string or contains
          at least one ``TextBlock`` — tool-result-only (and any other
          block-only) nodes are excluded.
        """
        from phoson_llm.schemas import TextBlock

        targets: list[tuple[str, str]] = []
        # Reverse walk → newest first (issue #109).
        for node in reversed(self._node_path()):
            message = node.message
            if message.role != "user" or node.parent_id is None:
                continue
            content = message.content
            if isinstance(content, str):
                text = content
            elif content:
                text = " ".join(b.text for b in content if isinstance(b, TextBlock))
                if not text:
                    # No TextBlock in block content: not a genuine user
                    # turn — the tool runner stores results as
                    # Message(role="user", content=[ToolResultBlock]) and
                    # those would render as "(empty message)" rows in the
                    # picker (issue #109).
                    continue
            else:
                text = ""
            # Whitespace-only *string* content is still a genuine (empty)
            # user turn — it keeps its "(empty message)" preview.
            preview = " ".join(text.split()) or "(empty message)"
            if len(preview) > 48:
                preview = preview[:47] + "…"
            targets.append((node.id, preview))
        return targets

    def jump_to_node(self, node_id: str) -> tuple[bool, str]:
        """Move the conversation cursor to ``node_id`` (G1 rewind primitive).

        The generalization of ``undo_last_turn``: set ``current_node_id``
        to any node in the tree and let the next user message branch from
        there. The "undone" messages are *not* deleted — they remain in
        the tree as an abandoned branch (visible via ``/tree``), and
        session cost/token metrics stay cumulative (intentionally NOT
        rolled back, same contract as ``/undo``).

        Returns:
            Tuple of (success, message or new node id).
        """
        if node_id not in self.tree.nodes:
            return False, f"Unknown node {node_id[:8]} — not in this session."
        self.current_node_id = node_id
        return True, node_id

    def jump_to_user_turn(self, user_node_id: str) -> tuple[bool, str]:
        """Rewind to just before a user turn (G1: the rewind picker lands here).

        Validates that the selected node is a user node on the *active*
        path (the picker only offers those — this is a defensive guard
        against a stale selection) and lands the cursor on its parent,
        so the next user message replaces the selected turn and
        everything after it.
        """
        node = self.tree.nodes.get(user_node_id)
        if node is None:
            return False, f"Unknown node {user_node_id[:8]} — not in this session."
        if node.parent_id is None:
            return False, "Nothing to rewind to — the session starts with this turn."
        if node.message.role != "user":
            return False, "Only user turns can be rewound."
        path_ids = {n.id for n in self._node_path()}
        if node.id not in path_ids:
            return False, (
                "That node is not on the active path — "
                "it belongs to an abandoned branch."
            )
        return self.jump_to_node(node.parent_id)

    def find_latest_node_id(self) -> str | None:
        """Find the most recent leaf node — the continuation point.

        Only leaves are considered (the next turn appends to a leaf), and
        ties on ``created_at`` are broken deterministically by node id so a
        loaded tree (nodes re-inserted in saved order) yields a stable pick.
        """
        if not self.tree.nodes:
            return None
        leaves = self.tree.get_leaves()
        if not leaves:
            return None
        leaf_nodes = [self.tree.nodes[node_id] for node_id in leaves]
        latest = max(leaf_nodes, key=lambda n: (n.created_at, n.id))
        return latest.id


# ── Helpers ────────────────────────────────────────────────────────────────────


def _text_block(text: str) -> "ContentBlock":
    """Create a TextBlock inline."""
    from phoson_llm.schemas import TextBlock

    return TextBlock(text=text)
