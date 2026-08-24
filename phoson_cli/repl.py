"""Classic interactive REPL (prompt_toolkit + Rich).

All session runtime — engine, tree, metrics, run lifecycle,
persistence — lives in :class:`~phoson_cli.controller.SessionController`,
which is free of UI dependencies. ``PhosonRepl`` is the *classic front
end* over that controller: it owns the prompt loop, key bindings,
completer, prompt fragments and banner, and adapts its Rich
``Renderer`` to the controller's
:class:`~phoson_cli.ui_protocols.AgentEventSink` via ``ClassicSink``.

A future full-screen front end will be a second front end over the
same controller — a sink, not a fork.
"""

import asyncio
import logging
from typing import Any
from pathlib import Path
from collections.abc import Iterable

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.document import Document
from prompt_toolkit.completion import Completer, Completion
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
from .commands import COMMANDS, COMMAND_SPECS, CommandHandler, parse_command
from .renderer import Renderer, ClassicSink
from .controller import SessionController
from .confirmation import PromptToolkitConfirmationService
from .ui_protocols import AgentEventSink, ConfirmationService
from .session_utils import (  # noqa: F401
    close_plugins,
    build_mcp_plugins,
    build_system_prompt,
)

_LOGGER = logging.getLogger("phoson_cli.repl")

# Sentinel distinguishing "confirmation not passed" (use the classic
# prompt_toolkit prompt) from an explicit ``confirmation=None`` (fail
# closed — e.g. the full-screen front end before it has its own modal).
_DEFAULT_CONFIRMATION: Any = object()

# Build a flat ``name -> help`` table from the central COMMAND_SPECS so the
# completer's meta column stays in sync with /help and the dispatch table.
_CMD_META: dict[str, str] = {
    name: spec.help for spec in COMMAND_SPECS for name in spec.names
}


class _SlashCompleter(Completer):
    """Completes slash commands only when the buffer starts with '/'."""

    def get_completions(
        self, document: Document, complete_event: object
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        # Only complete the command word itself (no args)
        if " " in text:
            return

        word = text.lower()
        for cmd in sorted(COMMANDS):
            if cmd.startswith(word):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=_CMD_META.get(cmd, ""),
                )


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
        self.theme: Theme = load_theme(getattr(config, "theme", None))
        self.renderer = Renderer(theme=self.theme)
        # Node ids whose reasoning has already been expanded this session
        # (the terminal is append-only, so a node's reasoning prints once).
        self._expanded_reasoning: set[str] = set()

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

    def branch_session(self) -> None:  # pragma: no cover - kept for API compat
        """Deprecated no-op kept for backward compatibility."""
        self._controller.branch_session()

    async def set_provider(self, provider: str) -> None:
        """Switch provider (models.json ``default_model`` honored)."""
        await self._controller.set_provider(provider)

    async def set_model(self, model: str) -> None:
        """Switch model and rebuild the engine."""
        await self._controller.set_model(model)

    def label_current_node(self, text: str) -> None:
        """Label the current node with text."""
        self._controller.label_current_node(text)

    def undo_last_turn(self) -> tuple[bool, str]:
        """Move the cursor back to just before the last user turn."""
        return self._controller.undo_last_turn()

    def find_latest_node_id(self) -> str | None:
        """Most recent leaf node — the continuation point."""
        return self._controller.find_latest_node_id()

    async def shutdown(self) -> None:
        """Release chat client and plugins (called on exit)."""
        await self._controller.shutdown()

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the REPL main loop.

        Displays the banner, initializes the prompt session, and processes
        user input until EOF or /exit command.
        """
        self._print_banner()

        history_path = Path("~/.phoson/history.txt").expanduser()
        history_path.parent.mkdir(parents=True, exist_ok=True)

        key_bindings = KeyBindings()

        @key_bindings.add("c-t")
        def _handle_ctrl_t(event: object) -> None:  # noqa: ARG001
            self._on_reasoning_toggle()

        session = PromptSession(
            history=FileHistory(str(history_path)),
            style=Style.from_dict(build_prompt_style(self.theme)),
            completer=_SlashCompleter(),
            complete_while_typing=True,
            reserve_space_for_menu=6,
            key_bindings=key_bindings,
        )
        command_handler = CommandHandler(self)

        while True:
            try:
                prompt_fragments = self._prompt_fragments()
                user_input = await session.prompt_async(FormattedText(prompt_fragments))
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
            ("", ""),
        ]

    def _token_indicator(self) -> str:
        """Return a short token usage string like '12.4k/128k'."""
        if self._context_window <= 0:
            return "?"
        used = self._context_tokens
        total = self._context_window

        def _fmt(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}k"
            return str(n)

        return f"{_fmt(used)}/{_fmt(total)}"

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
