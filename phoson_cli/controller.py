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
from dataclasses import dataclass

from phoson_agent import (
    Plugin,
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
)
from phoson_llm.schemas import Message, ModelConfig, ContentBlock
from phoson_agent.sessions import JsonlStorage, ConversationTree
from phoson_agent.plugins.summarizer import SummarizationMiddleware
from phoson_agent.plugins.context_window import ContextWindowResolver

from .tools import build_tools, build_tools_dict
from .config import PhosonConfig, build_chat
from .models import load_models_file, provider_settings, resolve_context_window
from ._session import SessionState, SessionMetrics
from .attachments import AttachmentManager
from .ui_protocols import AgentEventSink, ConfirmationService
from .session_utils import (
    close_plugins,
    build_mcp_plugins,
    build_system_prompt,
)

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
        # Sub-agent model: explicit override or fallback to main model.
        self.subagent_model: str = config.subagent_model or config.model

        # Context window resolver + token estimator for prompt display.
        self._cw_resolver = ContextWindowResolver(
            ollama_base_url=config.ollama_base_url or "http://localhost:11434",
            openrouter_api_key=config.openrouter_api_key,
        )
        self._context_window: int = 128_000  # default, resolved on first use
        self._context_tokens: int = 0  # current estimated tokens in context

        # Summarization middleware. The provider/model fields are kept in
        # sync with the active config every time ``_rebuild_engine`` runs.
        self.summarizer = SummarizationMiddleware(
            threshold=0.80,
            min_keep_messages=4,
            provider=config.provider,
            model=config.model,
            ollama_base_url=config.ollama_base_url or "http://localhost:11434",
            openrouter_api_key=config.openrouter_api_key,
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

    def _build_mcp_plugins(self) -> list[str | dict[str, Any] | Plugin]:
        """MCP plugin specs for the current configuration."""
        return build_mcp_plugins(self.config)

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

        plugins = self._build_mcp_plugins()

        self.engine = AgentEngine(
            chat=self.chat,
            tools=self.tools,
            middlewares=[self.summarizer],
            plugins=plugins,
            max_iterations=self.config.max_iterations,
        )

        # Inject runtime context for sub-agents.
        self.engine.context.extra["safe_mode"] = self.config.safe_mode
        self.engine.context.extra["available_tools"] = self.tools_dict
        self.engine.context.extra["default_model"] = self.subagent_model
        self.engine.context.extra["max_iterations"] = self.config.max_iterations
        self.engine.context.extra["subagent_max_parallel"] = (
            self.config.subagent_max_parallel
        )
        self.engine.context.extra["subagent_timeout_seconds"] = (
            self.config.subagent_timeout_seconds
        )
        self.engine.context.extra["chat"] = self.chat
        # Interactive confirmations (safe_mode bash). None → the tool
        # fails closed (one-shot / non-interactive front ends).
        self.engine.context.extra["bash_confirmation"] = self.confirmation

    async def shutdown(self) -> None:
        """Release the chat client and any loaded engine plugins.

        Idempotent. Called by front ends on exit (classic REPL on EOF,
        front end on shutdown) so no HTTP pools or MCP subprocesses
        outlive the session.
        """
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
        """Flush pending attachments and construct the user Message."""
        pending_blocks: list[ContentBlock] = []
        if self.attachments:
            media_blocks = list(self.attachments.flush())
            self.sink.on_attachments(
                [block.source.split("file://", 1)[-1] for block in media_blocks]
            )
            pending_blocks = list(media_blocks)

        if user_input:
            pending_blocks.insert(0, _text_block(user_input))

        content: str | list[ContentBlock] = (
            pending_blocks if pending_blocks else user_input
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

    def _finalize_run(self, done_event: AgentDoneEvent, base_count: int) -> None:
        """Append new messages to tree, update metrics."""
        new_messages = done_event.result.history[base_count:]
        if new_messages:
            created = self.tree.append_many(self.current_node_id, new_messages)
            self.current_node_id = created[-1].id

        self._context_tokens = self.summarizer.estimate_tokens(
            done_event.result.history
        )
        for step in done_event.result.steps:
            self.session_metrics.add_run_step(step)

    def _append_partial_history(self, base_count: int) -> None:
        """Slice engine partial history and append new nodes to the tree.

        Updates ``current_node_id`` to the last appended node.
        """
        partial = self.engine.get_partial_history()
        new_messages = partial[base_count:]
        if new_messages:
            created = self.tree.append_many(self.current_node_id, new_messages)
            self.current_node_id = created[-1].id

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
        await self.storage.save(self.tree)
        await self.storage.save_meta(
            self.tree.session_id, self.session_metrics.to_meta()
        )

    async def run_turn(self, user_input: str) -> RunOutcome:
        """Execute one agent turn (run, persist, notify the sink).

        All user-visible effects go through the sink; the tree, metrics
        and session storage are updated in every terminal state:

        - done:      new messages appended, reasoning persisted, saved.
        - error:     partial history + reasoning persisted, saved, and
                     an auth error produces an actionable warning.
        - cancelled: same as error (partial progress saved).
        """
        user_message = self._build_user_message(user_input)
        _node_id, path = self._append_user_turn(user_message)
        base_count = len(path)

        self.sink.on_user_message(user_input, user_message)

        config = ModelConfig(
            model=self.current_model,
            system=build_system_prompt(self.engine.tools),
            reasoning_effort=self.config.reasoning_effort,
        )

        # Resolve context window: models.json (user override or cache)
        # wins, then the engine's registry.
        _models_data = load_models_file()
        _model_bare = self.current_model.split("/", 1)[-1]
        _override = resolve_context_window(
            _models_data, self.config.provider, _model_bare
        )
        if _override is not None:
            self._context_window = _override
        else:
            self._context_window = await self._cw_resolver.resolve(
                self.config.provider, self.current_model
            )
        self._context_tokens = self.summarizer.estimate_tokens(path)

        try:
            terminal_event = await self._consume_stream(path, config)
        except asyncio.CancelledError:
            self.sink.flush_line()
            self.sink.capture_partial_reasoning()
            self._append_partial_history(base_count)
            self._persist_run_reasoning()
            await self._save_session()
            self.sink.notify("warn", "Partial progress saved.")
            return RunOutcome(status="cancelled")

        if isinstance(terminal_event, AgentErrorEvent):
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
        await self._save_session()
        return RunOutcome(
            status="done",
            final_content=terminal_event.result.final_content,
        )

    # ── Session / model management ────────────────────────────────────────

    def build_system_prompt(self) -> str:
        """System prompt for the next run (built-in + loaded MCP tools)."""
        return build_system_prompt(self.engine.tools)

    def new_session(self) -> None:
        """Start a fresh session, resetting tree and metrics."""
        self._session.reset()
        self.attachments.clear()
        self.sink.set_session(self._session.tree.session_id)

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

            # Replay the tail of the session so the user knows where they
            # left off.
            try:
                path = self.tree.get_path(self.current_node_id)
                self.sink.print_history(path, tail=6)
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

    def branch_session(self) -> None:  # pragma: no cover - kept for API compat
        """Deprecated no-op kept for backward compatibility."""
        pass

    def set_provider(self, provider: str) -> None:
        """Switch to a different provider and rebuild runtime state.

        If ``models.json`` defines a ``default_model`` for the new
        provider, that model is selected; otherwise the current model
        name is kept.
        """
        self.config.provider = provider
        settings = provider_settings(load_models_file(), provider)
        default_model = settings.get("default_model")
        self.set_model(default_model or self.config.model)

    def set_model(self, model: str) -> None:
        """Switch to a different model and rebuild the engine."""
        self.current_model = model
        self.config.model = model
        # Sub-agent model follows the main model unless explicitly overridden.
        self.subagent_model = self.config.subagent_model or model
        self._rebuild_engine()

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

        # Walk the node path root → cursor (the message-level get_path is
        # not enough: we need node ids to move the cursor).
        node_path: list = []
        cursor: str | None = self.current_node_id
        while cursor is not None:
            node = self.tree.nodes[cursor]
            node_path.append(node)
            cursor = node.parent_id
        node_path.reverse()

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
