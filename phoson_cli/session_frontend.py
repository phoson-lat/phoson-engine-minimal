"""Front-end-agnostic session facade shared by both interactive UIs.

:class:`SessionFrontend` wraps one
:class:`~phoson_cli.controller.SessionController` together with the small
amount of per-front-end *view* state every interactive UI needs (active
theme, already-expanded reasoning, update hint, terminal title). It is the
implementation of :class:`~phoson_cli.ui_protocols.SessionPort`, the
surface slash commands are written against.

- The classic REPL (:class:`~phoson_cli.repl.PhosonRepl`) subclasses it and
  adds the prompt_toolkit loop and the Rich ``Renderer``.
- The full-screen app (:class:`~phoson_cli.fullscreen.app.PhosonApp`) uses
  it directly with its own sink — it does not depend on the classic REPL.

This module is UI-free: no ``rich``/``prompt_toolkit`` imports.
"""

import asyncio
from typing import Any
from collections.abc import Callable, Coroutine

from phoson_agent import AgentEngine
from phoson_llm.schemas import TextBlock
from phoson_agent.sessions import JsonlStorage, ConversationTree

from .theme import Theme, ThemeRegistry, load_theme, resolve_runtime_theme
from .config import PhosonConfig
from ._session import SessionMetrics
from .plugin_ui import SinkPluginUiService
from .controller import RunOutcome, CompactPlan, SessionController
from .attachments import AttachmentManager
from .ui_protocols import AgentEventSink, ConfirmationService
from .command_catalog import CommandCatalog


class SessionFrontend:
    """One interactive session: controller + per-front-end view state.

    Args:
        config: Active configuration (mutated on model/provider switch).
        sink: Presentation target for the run stream.
        confirmation: Interactive confirmation service, or None to fail
            closed.
        theme: Startup theme; defaults to the configured built-in tier.
            Plugin themes are resolved once the controller has loaded
            plugins (see the end of ``__init__``).
    """

    def __init__(
        self,
        config: PhosonConfig,
        sink: AgentEventSink,
        confirmation: ConfirmationService | None,
        *,
        theme: Theme | None = None,
    ) -> None:
        self.theme: Theme = theme or config._startup_theme or load_theme()
        # Node ids whose reasoning has already been expanded this session
        # (the classic terminal is append-only, so a node's reasoning prints
        # once; the full-screen app tracks its expand state here too).
        self._expanded_reasoning: set[str] = set()
        # Startup update-check result (IMPROVEMENTS.md E5): the dim
        # "⬆ v0.8.1 available — /update" hint, or None when up to date /
        # offline / not due yet.
        self.update_hint: str | None = None
        self._update_check_task: asyncio.Task | None = None
        # Last terminal title we pushed (OSC 2) so a per-frame sync only
        # writes when the session title or run state actually changes.
        self._terminal_title: str | None = None
        self._controller = SessionController(config, sink, confirmation=confirmation)
        self.apply_theme(resolve_runtime_theme(config, self._controller.theme_registry))

    # ── Controller state ──────────────────────────────────────────────────

    @property
    def controller(self) -> SessionController:
        """The UI-independent session runtime behind this front end."""
        return self._controller

    @property
    def config(self) -> PhosonConfig:
        return self._controller.config

    @config.setter
    def config(self, value: PhosonConfig) -> None:
        self._controller.config = value

    @property
    def storage(self) -> JsonlStorage:
        return self._controller.storage

    @property
    def tree(self) -> ConversationTree:
        """The active conversation tree."""
        return self._controller.tree

    @tree.setter
    def tree(self, value: ConversationTree) -> None:
        self._controller.tree = value

    @property
    def current_node_id(self) -> str | None:
        """ID of the most recently active tree node."""
        return self._controller.current_node_id

    @current_node_id.setter
    def current_node_id(self, value: str | None) -> None:
        self._controller.current_node_id = value

    @property
    def session_metrics(self) -> SessionMetrics:
        """Accumulated metrics for the current session."""
        return self._controller.session_metrics

    @property
    def engine(self) -> AgentEngine:
        return self._controller.engine

    @engine.setter
    def engine(self, value: AgentEngine) -> None:
        self._controller.engine = value

    @property
    def chat(self):
        return self._controller.chat

    @chat.setter
    def chat(self, value) -> None:
        self._controller.chat = value

    @property
    def theme_registry(self) -> ThemeRegistry:
        """Themes contributed by the currently loaded plugin set."""
        return self._controller.theme_registry

    @property
    def command_catalog(self) -> CommandCatalog:
        """Native + plugin slash commands for the live engine."""
        return self._controller.command_catalog

    @property
    def plugin_ui(self) -> SinkPluginUiService:
        """Controller-scoped plugin UI service."""
        return self._controller.plugin_ui

    @property
    def sink(self) -> AgentEventSink:
        return self._controller.sink

    @property
    def tools(self):
        return self._controller.tools

    @property
    def tools_dict(self):
        return self._controller.tools_dict

    @property
    def current_model(self) -> str:
        return self._controller.current_model

    @current_model.setter
    def current_model(self, value: str) -> None:
        self._controller.current_model = value

    @property
    def subagent_model(self) -> str:
        return self._controller.subagent_model

    @subagent_model.setter
    def subagent_model(self, value: str) -> None:
        self._controller.subagent_model = value

    @property
    def attachments(self) -> AttachmentManager:
        return self._controller.attachments

    @property
    def summarizer(self):
        return self._controller.summarizer

    @property
    def context_window(self) -> int:
        return self._controller.context_window

    @property
    def context_tokens(self) -> int:
        return self._controller.context_tokens

    @property
    def current_task(self) -> asyncio.Task | None:
        """The in-flight run task (``None`` when idle)."""
        return self._controller.current_task

    @current_task.setter
    def current_task(self, value: asyncio.Task | None) -> None:
        self._controller.current_task = value

    @property
    def is_running(self) -> bool:
        """True while an agent run stream is being consumed."""
        return self._controller.is_running

    def cancel_current(self) -> bool:
        """Cancel the in-flight run, if any. Returns True if one was cancelled."""
        return self._controller.cancel_current()

    # ── Session operations ────────────────────────────────────────────────

    async def run_turn(self, user_input: str) -> RunOutcome:
        """Run one agent turn, marking the terminal title as working."""
        self.refresh_terminal_title(working=True)
        try:
            return await self._controller.run_turn(user_input)
        finally:
            self.refresh_terminal_title(working=False)

    def new_session(self) -> None:
        """Start a fresh session, resetting tree and metrics."""
        self._controller.new_session_now()
        self._expanded_reasoning.clear()

    async def load_session(self, session_id: str) -> bool:
        """Load a session from storage and replay its tail."""
        outcome = await self._controller.load_session(session_id)
        if outcome.ok:
            self._expanded_reasoning.clear()
        return outcome.ok

    async def compact_context(
        self, profile: str | None = None
    ) -> tuple[int, int, bool]:
        """Manually compact the conversation."""
        return await self._controller.compact_context(profile)

    def plan_compaction(self, profile: str | None = None) -> CompactPlan:
        """Preview what a compaction would do."""
        return self._controller.plan_compaction(profile)

    def set_compact_mode(self, mode: str) -> bool:
        """Switch automatic compaction mode. True when the mode was applied."""
        return self._controller.set_compact_mode(mode)

    async def set_provider(self, provider: str) -> None:
        """Switch provider (models.json ``default_model`` honored)."""
        await self._controller.set_provider(provider)

    async def set_model(
        self, model: str, provider: str | None = None, *, reuse_engine: bool = False
    ) -> None:
        """Switch model (and provider, when given).

        ``reuse_engine`` asks the controller for the in-place fast path: keep
        the plugin/tool/middleware layer and rebuild only the chat client when
        the provider changed (see :meth:`SessionController.set_model`).
        """
        await self._controller.set_model(
            model, provider=provider, reuse_engine=reuse_engine
        )

    def refresh_permission_policy(self) -> None:
        """Apply a changed ``permissions.json`` to the live gate."""
        self._controller.refresh_permission_policy()

    async def remember_bash_pattern(self, command: str) -> None:
        """Persist "always allow this exact command"."""
        await self._controller.remember_bash_pattern(command)

    def label_current_node(self, text: str) -> None:
        """Label the current node with text."""
        self._controller.label_current_node(text)

    def note_user_title(self) -> None:
        """Mark the session title as user-set (``/title``).

        A background LLM title must never overwrite a title the user chose.
        """
        self._controller.titles.note_user_title()

    def undo_last_turn(self) -> tuple[bool, str]:
        """Move the cursor back to just before the last user turn."""
        return self._controller.undo_last_turn()

    def jump_candidates(self) -> list[tuple[str, str]]:
        """Rewind targets for the double-Esc picker (G1): user turns on
        the active path, oldest first, as ``(node_id, preview)``."""
        return self._controller.jump_candidates()

    def jump_to_user_turn(self, user_node_id: str) -> tuple[bool, str]:
        """Rewind to just before the selected user turn (G1)."""
        return self._controller.jump_to_user_turn(user_node_id)

    def jump_to_node(self, node_id: str) -> tuple[bool, str]:
        """Move the cursor to any tree node (G1: undo a rewind jump)."""
        return self._controller.jump_to_node(node_id)

    def find_latest_node_id(self) -> str | None:
        """Most recent leaf node — the continuation point."""
        return self._controller.find_latest_node_id()

    def message_text(self, node_id: str) -> str:
        """Plain text of a node's message (empty string when missing).

        The full-screen rewind flow uses this to re-populate the composer
        with the text of the turn being rewound.
        """
        node = self._controller.tree.nodes.get(node_id)
        if node is None:
            return ""
        content = node.message.content
        if isinstance(content, str):
            return content
        if content:
            return " ".join(b.text for b in content if isinstance(b, TextBlock))
        return ""

    # ── View state ────────────────────────────────────────────────────────

    def apply_theme(self, theme: Theme) -> None:
        """Switch the active theme at runtime (IMPROVEMENTS.md E4).

        Front ends that own more theme consumers (renderer, prompt styles,
        header) extend this.
        """
        self.theme = theme
        plugin_ui = getattr(self._controller, "plugin_ui", None)
        if plugin_ui is not None:
            plugin_ui.set_theme(theme)

    def refresh_terminal_title(self, working: bool | None = None) -> None:
        """Sync the terminal window title with the session + run state.

        The title is the session title (falling back to ``phoson-cli``) and
        gets a ``*`` prefix while the agent is working — e.g. ``* Refactor X``.
        ``working`` defaults to the controller's live run state; pass it
        explicitly from a front end that tracks its own in-flight flag.
        Deduplicated against the last write so a per-frame caller is cheap.
        """
        from .terminal_title import set_title, format_title

        is_working = self._controller.is_running if working is None else working
        title = format_title(self.tree.title, is_working)
        if title == self._terminal_title:
            return
        self._terminal_title = title
        set_title(title)

    async def shutdown(self) -> None:
        """Release chat client and plugins (called on exit)."""
        task = self._update_check_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._controller.shutdown()

    # ── Startup update check (IMPROVEMENTS.md E5) ─────────────────────────

    def start_update_check(self, on_settle: Callable[[], None] | None = None) -> None:
        """Kick off the non-blocking startup PyPI check (E5).

        The check runs as a plain background task on the running loop: it
        must not delay the first paint, input, or a run, and it may still
        be in flight when the session exits (``shutdown`` cancels it).
        The check itself enforces the once-per-day cadence (and retries
        after a failed attempt) — the front end only renders the hint.
        ``on_settle`` (optional) is invoked once the hint is known, so a
        front end that repaints on demand (full-screen) can refresh the
        header the moment the check lands.
        """
        from .updater import check_for_startup_update

        self._update_check_task = asyncio.get_running_loop().create_task(
            self._store_update_check(check_for_startup_update(), on_settle)
        )

    async def _store_update_check(
        self,
        check: Coroutine[Any, Any, str | None],
        on_settle: Callable[[], None] | None = None,
    ) -> None:
        """Await the check and store its result; failure means no hint."""
        from .updater import update_hint

        try:
            latest = await check
        except Exception:  # noqa: BLE001
            latest = None
        self.update_hint = update_hint(latest) if latest else None
        if on_settle is not None:
            try:
                on_settle()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["SessionFrontend"]
