"""Classic interactive REPL (prompt_toolkit + Rich).

All session runtime — engine, tree, metrics, run lifecycle,
persistence — lives in :class:`~phoson_cli.controller.SessionController`,
which is free of UI dependencies; the view state both front ends share
lives in :class:`~phoson_cli.session_frontend.SessionFrontend`.
``PhosonRepl`` is the *classic front end* on top of that: it owns the
prompt loop, key bindings, completer, prompt fragments and banner, and
adapts its Rich ``Renderer`` to the controller's
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

import sys
import asyncio
import logging
from typing import Any
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import FormattedText

from phoson_llm.schemas import REASONING_EFFORTS

from .theme import Theme, load_theme, build_prompt_style
from ._views import print_banner, render_tree_ascii
from .config import PhosonConfig, save_config, enabled_providers_from_config
from .commands import (
    PathCompleter,
    CommandHandler,
    SlashCompleter,
    parse_command,
)
from .renderer import Renderer, ClassicSink
from .terminal import stream_is_tty, cursor_output_capable
from .formatting import format_token_indicator
from .model_cache import ModelCache
from .confirmation import PromptToolkitConfirmationService
from .ui_protocols import AgentEventSink, ConfirmationService
from .session_cache import SessionListCache
from .arg_completers import (
    ModelArgCompleter,
    ResumeArgCompleter,
    StaticArgCompleter,
    SessionsArgCompleter,
)
from .session_frontend import SessionFrontend

_LOGGER = logging.getLogger("phoson_cli.repl")

# Sentinel distinguishing "confirmation not passed" (use the classic
# prompt_toolkit prompt) from an explicit ``confirmation=None`` (fail
# closed — e.g. the full-screen front end before it has its own modal).
_DEFAULT_CONFIRMATION: Any = object()

#: `/mcp <subcommand>` values, for inline argument autocomplete (see
#: ``phoson_cli._mcp_commands._MCPSubcommands.dispatch``).
_MCP_SUBCOMMANDS: tuple[str, ...] = (
    "status",
    "init",
    "enable",
    "disable",
    "toggle",
    "config",
    "help",
)


class PhosonRepl(SessionFrontend):
    """Classic interactive REPL for the Phoson agent platform.

    prompt_toolkit front end over :class:`SessionFrontend` (which owns the
    :class:`~phoson_cli.controller.SessionController` and the shared view
    state). This class adds the prompt loop, completers, banner and the
    Rich ``Renderer``.
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
                ``ClassicSink(self.renderer)``.
            confirmation: Interactive yes/no service (bash safe_mode).
                Defaults to a prompt_toolkit prompt when not passed at
                all; pass ``None`` explicitly to fail closed (no
                confirmation available), or a modal-based implementation.
        """
        # Theme is resolved once at startup (env NO_COLOR/PHOSON_THEME, then
        # config.toml, then dark) and shared by every rendering site.
        # Plugin-provided themes cannot be known until the controller has
        # loaded plugins: start with a safe built-in tier; SessionFrontend
        # resolves the configured theme against the per-session registry.
        theme = config._startup_theme or load_theme()
        self.renderer = Renderer(
            theme=theme,
            show_reasoning=getattr(config, "show_reasoning", True),
            tool_detail=getattr(config, "tool_detail", "full"),
        )
        self.picker_capable = stream_is_tty(sys.stdin) and cursor_output_capable(
            self.renderer.console.file
        )
        # Inline argument autocomplete (same pattern as the full-screen TUI):
        # the model/session lists are fetched in the background so typing
        # `/model ` or `/resume ` never blocks on a network round trip.
        self._model_cache = ModelCache()
        self._session_cache = SessionListCache()
        # Text queued by plugin dictation (Ctrl+O) to appear pre-filled in the
        # next prompt; consumed by the next prompt_async.
        self._prefill_text: str = ""
        super().__init__(
            config,
            sink if sink is not None else ClassicSink(self.renderer),
            (
                PromptToolkitConfirmationService()
                if confirmation is _DEFAULT_CONFIRMATION
                else confirmation
            ),
            theme=theme,
        )

    def request_prefill(self, text: str) -> None:
        """Queue *text* for the next classic prompt buffer.

        Used by plugin dictation (``Ctrl+O``) so dictated text shows up in the
        input for the user to review, edit and submit, instead of only being
        printed. The value is consumed by the next ``prompt_async`` call.
        """
        self._prefill_text = text

    def apply_theme(self, theme: Theme) -> None:
        """Switch the active theme at runtime (IMPROVEMENTS.md E4).

        Extends :meth:`SessionFrontend.apply_theme` with the classic
        renderer and its subagent spinner (the only component that captured
        its own copy). Rich renderables are built with the theme's tokens,
        so the next render picks the new palette up automatically — and the
        no-color tier's empty tokens yield plain output without any
        console-level fiddling.
        """
        super().apply_theme(theme)
        self.renderer.theme = theme
        self.renderer._spinner._enabled = (
            theme.name != "no-color" and self.renderer._spinner._detect_enabled()
        )
        self.renderer._subagent_spinner._theme = theme
        self.renderer._subagent_spinner._enabled = (
            self.renderer._subagent_spinner._detect_enabled()
        )

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

        # Autonomous monitor wake loop (I-126): needs the running event
        # loop, so it starts here (the controller __init__ runs before it
        # in the classic REPL). No-op when enable_monitors is off.
        self.controller.start_monitor_wake_loop()

        key_bindings = KeyBindings()

        @key_bindings.add("c-t")
        def _handle_ctrl_t(event: object) -> None:  # noqa: ARG001
            self._on_reasoning_toggle()

        @key_bindings.add("c-e")
        def _handle_ctrl_e(event: object) -> None:  # noqa: ARG001
            self._cycle_reasoning_effort()

        session = PromptSession(
            history=FileHistory(str(history_path)),
            style=Style.from_dict(build_prompt_style(self.theme)),
            # Slash commands, inline argument autocomplete (the same
            # completers the full-screen app wires, so both front ends
            # behave identically) and @file mentions (E3).
            completer=merge_completers(
                [
                    SlashCompleter(lambda: self.command_catalog),
                    ModelArgCompleter(self._model_cache),
                    StaticArgCompleter(
                        ("/reasoning-effort ", "/effort "),
                        [*REASONING_EFFORTS, "off"],
                    ),
                    StaticArgCompleter(
                        ("/provider ",),
                        lambda: enabled_providers_from_config(self.config),
                    ),
                    StaticArgCompleter(
                        ("/theme ",),
                        lambda: list(self.theme_registry.valid_names()),
                    ),
                    StaticArgCompleter(("/mcp ",), list(_MCP_SUBCOMMANDS)),
                    SessionsArgCompleter(self._session_cache),
                    ResumeArgCompleter(self._session_cache),
                    PathCompleter(),
                ]
            ),
            complete_while_typing=True,
            reserve_space_for_menu=6,
            key_bindings=key_bindings,
        )
        # Prefetch the model/session lists for inline autocomplete without
        # delaying the first prompt (same as the full-screen front end).
        asyncio.create_task(self._model_cache.refresh(self.config))
        asyncio.create_task(
            self._session_cache.refresh(self.storage, cwd=str(Path.cwd()))
        )
        command_handler = CommandHandler(self)

        while True:
            try:
                prompt_fragments = self._prompt_fragments()
                # Keep the terminal window title in sync (picks up an async
                # LLM title or /title that landed while idle) before prompting.
                self.refresh_terminal_title(working=False)
                # Text queued by plugin dictation (Ctrl+O): shown
                # pre-filled so the user can review/edit before sending.
                prefill, self._prefill_text = self._prefill_text, ""
                # Per-pass style (E4): a /theme switch mid-session must
                # re-color the prompt on the next pass without rebuilding
                # the session — prompt_async accepts a style override.
                user_input = await session.prompt_async(
                    FormattedText(prompt_fragments),
                    style=Style.from_dict(build_prompt_style(self.theme)),
                    default=prefill,
                )
            except KeyboardInterrupt:
                if self.is_running:
                    self.cancel_current()
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
                await self.run_turn(text)
            except KeyboardInterrupt:
                if self.is_running:
                    self.cancel_current()
                    self.renderer.print_warn("Interrupted — run cancelled.")

    # ── Reasoning (Ctrl+T) ─────────────────────────────────────────────────

    def _cycle_reasoning_effort(self) -> None:
        """Ctrl+E handler: cycle reasoning effort off → low → … → max.

        Mirrors the full-screen TUI (``fullscreen/state_cycles.py``): the
        level is persisted and applies from the next turn.
        """
        if self.is_running:
            self.renderer.print_warn("Wait for the current operation to finish.")
            return
        current = self.config.reasoning_effort
        if current not in REASONING_EFFORTS:
            current = None  # "off"
        levels = (*REASONING_EFFORTS, None)
        next_effort = levels[(levels.index(current) + 1) % len(levels)]
        self.config.reasoning_effort = next_effort
        save_config(self.config, only_fields={"reasoning_effort"})
        self.renderer.print_info(
            f"Reasoning effort → {next_effort or 'off'}"
            "  ·  applies from the next turn (explicit: /reasoning-effort)"
        )

    def _on_reasoning_toggle(self) -> None:
        """Ctrl+T handler.

        While a run is streaming, toggles the live "thinking" panel on
        the fly. Otherwise expands the reasoning of the newest node on
        the current path that has any (a node's reasoning is printed at
        most once per REPL session — the terminal is append-only).
        """
        if self.is_running:
            new_state = self.renderer.toggle_live_reasoning()
            # Persist the choice like the full-screen front end does
            # (state_cycles.toggle_reasoning): Ctrl+T is a durable
            # show_reasoning preference, not just a per-run toggle.
            if getattr(self.config, "show_reasoning", True) != new_state:
                self.config.show_reasoning = new_state
                save_config(self.config, only_fields={"show_reasoning"})
            return

        cursor: str | None = self.current_node_id
        path_ids: list[str] = []
        while cursor is not None:
            path_ids.append(cursor)
            node = self.tree.nodes.get(cursor)
            cursor = node.parent_id if node is not None else None
        found_reasoning = False
        for node_id in path_ids:
            node = self.tree.nodes.get(node_id)
            reasoning = node.metadata.get("reasoning") if node else None
            if not reasoning:
                continue
            found_reasoning = True
            if node_id in self._expanded_reasoning:
                continue
            self.renderer.console.print(
                self.renderer.render_reasoning_panel(str(reasoning))
            )
            self._expanded_reasoning.add(node_id)
            return

        if found_reasoning:
            self.renderer.print_info(
                "Reasoning already expanded (the terminal is append-only)."
            )
        else:
            self.renderer.print_info(
                "No reasoning captured in the current conversation."
            )

    # ── Tree rendering ────────────────────────────────────────────────────

    def render_tree_ascii(self) -> str:
        """Render the conversation tree as an ASCII diagram."""
        return render_tree_ascii(self.tree, self.current_node_id)

    # ── Prompt ────────────────────────────────────────────────────────────

    def _prompt_fragments(self) -> list[tuple[str, str]]:
        """Return prompt_toolkit (style, text) fragments for the input prompt."""
        # Show the full saved model id (with the vendor/ prefix, e.g.
        # "anthropic/claude-opus-4.6") so the prompt matches config.toml
        # and the session's last_model — the full-screen header already
        # shows the full id, and the two front ends must not disagree
        # about which model is active.
        short_model = self.current_model
        short_node = (self.current_node_id or "new")[:8]
        # Show pending attachments indicator
        attach_indicator = f" 📎{len(self.attachments)}" if self.attachments else ""

        # Token context indicator
        token_part = self._token_indicator()

        # Active-monitors indicator (I-126): the plugin reports it via a
        # duck-typed hook on the controller; shown dim in the prompt,
        # alongside model/node/tokens. In-memory, safe on every prompt.
        monitors_part = self.controller.monitor_status() or ""

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
            ("class:prompt.tokens", f" · {monitors_part}" if monitors_part else ""),
            ("class:prompt.bracket", "]"),
            ("class:prompt.arrow", " › "),
            ("class:prompt.update", update_part),
            ("", ""),
        ]

    def _token_indicator(self) -> str:
        """Return a short token usage string like '12.4k/128k'."""
        return format_token_indicator(self.context_tokens, self.context_window)

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
