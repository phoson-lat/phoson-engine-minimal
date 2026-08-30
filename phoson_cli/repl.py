"""Classic interactive REPL (prompt_toolkit + Rich).

All session runtime — engine, tree, metrics, run lifecycle,
persistence — lives in :class:`~phoson_cli.controller.SessionController`,
which is free of UI dependencies. ``PhosonRepl`` is the *classic front
end* over that controller: it owns the prompt loop, key bindings,
completer, prompt fragments and banner, and adapts its Rich
``Renderer`` to the controller's
:class:`~phoson_cli.ui_protocols.AgentEventSink` via ``ClassicSink``.

**Status (IMPROVEMENTS.md D2):** the full-screen front end
(``phoson_cli.fullscreen.app.PhosonApp``) is the default interactive
experience. This classic REPL is the *retained degraded mode*: it is
user-facing via ``phoson-cli --classic`` (or ``--no-fullscreen``) and is
selected automatically when the interactive terminal cannot do
full-screen (``TERM`` unset or ``dumb``). It stays fully maintained as
a second front end over the same controller — a sink, not a fork — and
is the home of the classic rendering primitives (``Renderer``,
``ClassicSink``) that both front ends share where possible.
"""

import asyncio
import logging
from typing import Any
from pathlib import Path
from collections.abc import Callable, Coroutine

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import FormattedText

# Re-exported for backward compatibility (one-shot mode, existing tests).
from phoson_agent import AgentEngine  # noqa: F401
from phoson_agent.sessions import ConversationTree  # noqa: F401

from .theme import Theme, load_theme, build_prompt_style
from ._views import print_banner, render_tree_ascii
from .config import (
    PhosonConfig,
    build_chat,  # noqa: F401
)
from ._session import SessionMetrics  # noqa: F401
from .commands import (
    PathCompleter,
    CommandHandler,
    SlashCompleter,
    parse_command,
)
from .renderer import Renderer, ClassicSink
from .controller import SessionController
from .formatting import format_token_indicator
from .confirmation import PromptToolkitConfirmationService
from .ui_protocols import AgentEventSink, ConfirmationService
from .session_utils import (  # noqa: F401
    close_plugins,
    build_mcp_plugins,
    build_plugin_specs,
    build_system_prompt,
)

_LOGGER = logging.getLogger("phoson_cli.repl")

# Sentinel distinguishing "confirmation not passed" (use the classic
# prompt_toolkit prompt) from an explicit ``confirmation=None`` (fail
# closed — e.g. the full-screen front end before it has its own modal).
_DEFAULT_CONFIRMATION: Any = object()


class PhosonRepl:
    """Classic interactive REPL for the Phoson agent platform.

    Thin prompt_toolkit front end over
    :class:`~phoson_cli.controller.SessionController`. The public API
    (methods and state attributes) is preserved for the command handlers
    and the existing test suite.
    """

    def __init__(
        self,
        config: PhosonConfig,
        sink: AgentEventSink | None = None,
        confirmation: ConfirmationService | None = _DEFAULT_CONFIRMATION,
    ) -> None:
        """Initialize the REPL with configuration.

        Args:
            config: PhosonConfig containing provider, model, and session settings.
            sink: Presentation target for the session run. Defaults to
                ``ClassicSink(self.renderer)`` — a full-screen front end
                injects its own sink instead.
            confirmation: Interactive yes/no service (bash safe_mode).
                Defaults to a prompt_toolkit prompt when not passed at
                all; pass ``None`` explicitly to fail closed (no
                confirmation available), or a modal-based implementation.
        """
        self._config = config
        # Theme is resolved once at startup (env NO_COLOR/PHOSON_THEME, then
        # config.toml, then dark) and shared by every rendering site.
        # Plugin-provided themes cannot be known until the controller has
        # loaded plugins. Start with a safe built-in tier, then resolve the
        # configured theme against its per-session registry below.
        self.theme: Theme = load_theme()
        self.renderer = Renderer(theme=self.theme)
        # Node ids whose reasoning has already been expanded this session
        # (the terminal is append-only, so a node's reasoning prints once).
        self._expanded_reasoning: set[str] = set()
        # Startup update-check result (IMPROVEMENTS.md E5): the dim
        # "⬆ v0.8.1 available — /update" hint rendered in the prompt line,
        # or None when up to date / offline / not due yet.
        self.update_hint: str | None = None
        self._update_check_task: asyncio.Task | None = None

        # The session runtime — engine, tree, metrics, run lifecycle —
        # lives in the UI-independent controller; this REPL is its
        # prompt_toolkit front end.
        self._controller = SessionController(
            config,
            sink if sink is not None else ClassicSink(self.renderer),
            confirmation=(
                PromptToolkitConfirmationService()
                if confirmation is _DEFAULT_CONFIRMATION
                else confirmation
            ),
        )
        self.apply_theme(
            load_theme(config.theme, registry=self._controller.theme_registry)
        )

    # ── Config / controller state ─────────────────────────────────────────

    @property
    def config(self) -> PhosonConfig:
        return self._controller.config

    @config.setter
    def config(self, value: PhosonConfig) -> None:
        self._controller.config = value
        self._config = value

    @property
    def storage(self):
        return self._controller.storage

    @property
    def tree(self) -> "ConversationTree":
        """The active conversation tree."""
        return self._controller.tree

    @tree.setter
    def tree(self, value: "ConversationTree") -> None:
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
    def engine(self):
        return self._controller.engine

    @engine.setter
    def engine(self, value) -> None:
        self._controller.engine = value

    @property
    def chat(self):
        return self._controller.chat

    @chat.setter
    def chat(self, value) -> None:
        self._controller.chat = value

    @property
    def theme_registry(self):
        """Themes contributed by the currently loaded plugin set."""
        return self._controller.theme_registry

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
    def attachments(self):
        return self._controller.attachments

    @property
    def summarizer(self):
        return self._controller.summarizer

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

    # ── Private-state passthroughs (tests, prompt display) ─────────────────

    @property
    def _cw_resolver(self):
        return self._controller._cw_resolver

    @property
    def _context_window(self) -> int:
        return self._controller.context_window

    @_context_window.setter
    def _context_window(self, value: int) -> None:
        self._controller._context_window = value

    @property
    def _context_tokens(self) -> int:
        return self._controller.context_tokens

    @_context_tokens.setter
    def _context_tokens(self, value: int) -> None:
        self._controller._context_tokens = value

    # ── Delegated runtime methods ─────────────────────────────────────────

    def _rebuild_engine(self) -> None:
        self._controller._rebuild_engine()

    def _build_system_prompt(self) -> str:
        return self._controller.build_system_prompt()

    def _build_user_message(self, user_input: str):
        return self._controller._build_user_message(user_input)

    def _append_user_turn(self, message):
        return self._controller._append_user_turn(message)

    def _finalize_run(self, done_event, base_count: int) -> None:
        self._controller._finalize_run(done_event, base_count)

    def _append_partial_history(self, base_count: int) -> None:
        self._controller._append_partial_history(base_count)

    async def _run_agent(self, user_input: str):
        """Run one agent turn (delegates to the controller)."""
        return await self._controller.run_turn(user_input)

    def new_session(self) -> None:
        """Start a fresh session, resetting tree and metrics."""
        self._controller.new_session()

    async def load_session(self, session_id: str) -> bool:
        """Load a session from storage and replay its tail."""
        outcome = await self._controller.load_session(session_id)
        return outcome.ok

    async def compact_context(
        self, profile: str | None = None
    ) -> tuple[int, int, bool]:
        """Manually compact the conversation (delegates to the controller)."""
        return await self._controller.compact_context(profile)

    def plan_compaction(self, profile: str | None = None):
        """Preview what a compaction would do (delegates to the controller)."""
        return self._controller.plan_compaction(profile)

    def set_compact_mode(self, mode: str) -> bool:
        """Switch automatic compaction mode (delegates to the controller).

        Returns True when the mode was applied.
        """
        return self._controller.set_compact_mode(mode)

    async def set_provider(self, provider: str) -> None:
        """Switch provider (models.json ``default_model`` honored)."""
        await self._controller.set_provider(provider)

    def apply_theme(self, theme: Theme) -> None:
        """Switch the active theme at runtime (IMPROVEMENTS.md E4).

        Re-points every theme consumer owned by this front end: this
        front end's ``theme`` attribute, the shared renderer, and its
        subagent spinner (the only component that captured its own
        copy). Rich renderables are built with the theme's tokens, so the
        next render picks the new palette up automatically — and the
        no-color tier's empty tokens yield plain output without any
        console-level fiddling. The full-screen shell wraps this with
        its own repaint path (``PhosonApp.apply_theme``) because it owns
        additional style consumers (prompt_toolkit styles, header, sink).
        """
        self.theme = theme
        self.renderer.theme = theme
        self.renderer._subagent_spinner._theme = theme

    async def set_model(self, model: str, provider: str | None = None) -> None:
        """Switch model (and provider, when given) and rebuild the engine."""
        await self._controller.set_model(model, provider=provider)

    def label_current_node(self, text: str) -> None:
        """Label the current node with text."""
        self._controller.label_current_node(text)

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

    def message_text(self, node_id: str) -> str:
        """Plain text of a node's message (empty string when missing).

        The full-screen rewind flow uses this to re-populate the
        composer with the text of the turn being rewound.
        """
        from phoson_llm.schemas import TextBlock

        node = self._controller.tree.nodes.get(node_id)
        if node is None:
            return ""
        content = node.message.content
        if isinstance(content, str):
            return content
        if content:
            return " ".join(b.text for b in content if isinstance(b, TextBlock))
        return ""

    def find_latest_node_id(self) -> str | None:
        """Most recent leaf node — the continuation point."""
        return self._controller.find_latest_node_id()

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

    # ── Main loop ─────────────────────────────────────────────────────────

    def _history_path(self) -> Path:
        """Return the configured shared input-history path.

        ``history_file`` is optional for compatibility with legacy config
        objects used by integrations and tests; those use the historical
        default path.
        """
        history_file = getattr(self.config, "history_file", None)
        if history_file:
            return Path(history_file)
        return Path("~/.phoson/history.txt").expanduser()

    async def run(self) -> None:
        """Run the REPL main loop.

        Displays the banner, initializes the prompt session, and processes
        user input until EOF or /exit command.
        """
        self._print_banner()

        history_path = self._history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)

        # Startup PyPI update check (IMPROVEMENTS.md E5): a background
        # task that never blocks the first prompt or any key press. It
        # re-queries at most once per day (cache in
        # ``~/.phoson/last_update_check``) and, when a newer release
        # exists, sets ``self.update_hint`` which the prompt line renders
        # dimly ("⬆ v0.8.1 available — /update").
        self.start_update_check()

        key_bindings = KeyBindings()

        @key_bindings.add("c-t")
        def _handle_ctrl_t(event: object) -> None:  # noqa: ARG001
            self._on_reasoning_toggle()

        session = PromptSession(
            history=FileHistory(str(history_path)),
            style=Style.from_dict(build_prompt_style(self.theme)),
            # Slash commands plus @file mentions (E3) — the same two
            # completers the full-screen app uses, so both front ends
            # behave identically.
            completer=merge_completers(
                [
                    SlashCompleter(lambda: self._controller.command_catalog),
                    PathCompleter(),
                ]
            ),
            complete_while_typing=True,
            reserve_space_for_menu=6,
            key_bindings=key_bindings,
        )
        command_handler = CommandHandler(self)

        while True:
            try:
                prompt_fragments = self._prompt_fragments()
                # Per-pass style (E4): a /theme switch mid-session must
                # re-color the prompt on the next pass without rebuilding
                # the session — prompt_async accepts a style override.
                user_input = await session.prompt_async(
                    FormattedText(prompt_fragments),
                    style=Style.from_dict(build_prompt_style(self.theme)),
                )
            except KeyboardInterrupt:
                if self._controller.is_running:
                    self._controller.cancel_current()
                    self.renderer.print_warn("Interrupted — run cancelled.")
                continue
            except EOFError:
                await self.shutdown()
                self.renderer.print_info("Bye.")
                return

            text = user_input.strip()
            if not text:
                continue

            cmd = parse_command(text)
            if cmd:
                should_continue = await command_handler.handle(cmd)
                if not should_continue:
                    await self.shutdown()
                    self.renderer.print_info("Bye.")
                    return
                continue

            try:
                await self._run_agent(text)
            except KeyboardInterrupt:
                if self._controller.is_running:
                    self._controller.cancel_current()
                    self.renderer.print_warn("Interrupted — run cancelled.")

    # ── Reasoning (Ctrl+T) ─────────────────────────────────────────────────

    def _on_reasoning_toggle(self) -> None:
        """Ctrl+T handler.

        While a run is streaming, toggles the live "thinking" panel on
        the fly. Otherwise expands the reasoning of the newest node on
        the current path that has any (a node's reasoning is printed at
        most once per REPL session — the terminal is append-only).
        """
        if self._controller.is_running:
            self.renderer.toggle_live_reasoning()
            return

        cursor: str | None = self.current_node_id
        path_ids: list[str] = []
        while cursor is not None:
            path_ids.append(cursor)
            node = self.tree.nodes.get(cursor)
            cursor = node.parent_id if node is not None else None
        path_ids.reverse()

        for node_id in path_ids:
            node = self.tree.nodes.get(node_id)
            reasoning = node.metadata.get("reasoning") if node else None
            if not reasoning:
                continue
            if node_id in self._expanded_reasoning:
                self.renderer.print_info(
                    "Reasoning already expanded (the terminal is append-only)."
                )
                return
            self._expanded_reasoning.add(node_id)
            self.renderer.console.print(
                self.renderer.render_reasoning_panel(str(reasoning))
            )
            return

        self.renderer.print_info("No reasoning captured in the current conversation.")

    # ── Tree rendering ────────────────────────────────────────────────────

    def render_tree_ascii(self) -> str:
        """Render the conversation tree as an ASCII diagram."""
        return render_tree_ascii(self.tree, self.current_node_id)

    # ── Prompt ────────────────────────────────────────────────────────────

    def _prompt_fragments(self) -> list[tuple[str, str]]:
        """Return prompt_toolkit (style, text) fragments for the input prompt."""
        short_model = self.current_model.split("/")[-1][:22]
        short_node = (self.current_node_id or "new")[:8]
        # Show pending attachments indicator
        attach_indicator = f" 📎{len(self.attachments)}" if self.attachments else ""

        # Token context indicator
        token_part = self._token_indicator()

        # Update-available hint (IMPROVEMENTS.md E5) — a dim, single-line
        # "⬆ v0.8.1 available — /update" slot appended to the prompt. It
        # appears as soon as the background PyPI check lands and never
        # blocks the prompt or the paint.
        update_part = f"·{self.update_hint}" if self.update_hint else ""

        return [
            ("class:prompt.prefix", "phoson"),
            ("class:prompt.bracket", " ["),
            ("class:prompt.model", short_model),
            ("class:prompt.sep", "·"),
            ("class:prompt.node", short_node),
            ("class:prompt.sep", attach_indicator),
            ("class:prompt.sep", "·"),
            ("class:prompt.tokens", token_part),
            ("class:prompt.bracket", "]"),
            ("class:prompt.arrow", " › "),
            ("class:prompt.update", update_part),
            ("", ""),
        ]

    def _token_indicator(self) -> str:
        """Return a short token usage string like '12.4k/128k'."""
        return format_token_indicator(self._context_tokens, self._context_window)

    # ── Banner ────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        """Render the welcome banner."""
        print_banner(
            self.renderer.console,
            provider=self.config.provider,
            model=self.current_model,
            session_id=self.tree.session_id,
            theme=self.theme,
        )
