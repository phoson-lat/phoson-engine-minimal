"""Command handler for the Phoson CLI.

Each slash command lives in its own ``_cmd_*`` method. The
:class:`CommandHandler` builds a dispatch table at construction time
(``{cmd_name: handler}``) so that ``handle()`` is a flat lookup rather
than a 250-line cascade of ``if/elif`` branches. The :data:`COMMAND_SPECS`
list is the single source of truth for command names, aliases and
help strings — both ``/help`` and the slash-completer in the REPL read
from it.

To add a new command:

  1. Write ``_cmd_foo(self, cmd: Command) -> bool``.
  2. Append a :class:`CommandSpec` entry to :data:`COMMAND_SPECS`.

That's it; the dispatch table picks it up automatically.
"""

import inspect
from typing import TYPE_CHECKING, Any, Final
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from .config import save_config, enabled_providers_from_config
from .updater import perform_self_update
from .installer import run_install_wizard  # noqa: F401 - patched by tests / host
from .attachments import provider_compat_warning
from .command_host import CommandHost, RendererCommandHost
from .model_picker import pick_model  # noqa: F401 - patched by tests / host
from ._mcp_commands import _MCPSubcommands
from .model_selector import list_available_models
from .provider_picker import pick_provider  # noqa: F401 - patched by tests / host

if TYPE_CHECKING:
    from phoson_agent.sessions.models import SessionMeta

    from .repl import PhosonRepl


# ─── Command spec ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandSpec:
    """Metadata for a single slash command.

    Attributes:
        names: Canonical name plus aliases (all start with ``/``).
        help: One-line help string shown in ``/help``.
        method: Name of the ``CommandHandler`` method that implements it.
    """

    names: tuple[str, ...]
    help: str
    method: str

    @property
    def primary(self) -> str:
        return self.names[0]


CommandHandlerFn = Callable[["CommandHandler", "Command"], Awaitable[bool]]


# Order here is the order they appear in ``/help``.
COMMAND_SPECS: Final[tuple[CommandSpec, ...]] = (
    CommandSpec(("/exit", "/quit"), "Exit the REPL", "_cmd_exit"),
    CommandSpec(("/new", "/clear"), "Start a new session", "_cmd_new"),
    CommandSpec(("/model",), "Pick or set the active model", "_cmd_model"),
    CommandSpec(("/provider",), "Pick or set the active provider", "_cmd_provider"),
    CommandSpec(
        ("/subagent-model",),
        "Pick or set the model used by sub-agents",
        "_cmd_subagent_model",
    ),
    CommandSpec(
        ("/reasoning-effort", "/effort"),
        "Show or set reasoning effort: low, medium, high, or off",
        "_cmd_reasoning_effort",
    ),
    CommandSpec(("/tree",), "Show the conversation tree as ASCII", "_cmd_tree"),
    CommandSpec(
        ("/sessions",), "List, load (#) or pick saved sessions", "_cmd_sessions"
    ),
    CommandSpec(("/delete",), "Delete a session by id", "_cmd_delete"),
    CommandSpec(("/label",), "Label the current node with a short name", "_cmd_label"),
    CommandSpec(
        ("/title",), "Set a human-readable title for this session", "_cmd_title"
    ),
    CommandSpec(
        ("/undo",),
        "Undo the last turn (branch from before your last message)",
        "_cmd_undo",
    ),
    CommandSpec(
        ("/attach", "/attachments"),
        "Attach a file to the next message, or list pending attachments",
        "_cmd_attach",
    ),
    CommandSpec(("/help",), "Show this help", "_cmd_help"),
    CommandSpec(("/env",), "Show provider, model and session info", "_cmd_env"),
    CommandSpec(("/cost",), "Show running cost in USD/credits", "_cmd_cost"),
    CommandSpec(("/tokens",), "Show running input/output token totals", "_cmd_tokens"),
    CommandSpec(("/steps",), "Show the number of agent steps so far", "_cmd_steps"),
    CommandSpec(("/setup",), "Run the initial setup wizard again", "_cmd_setup"),
    CommandSpec(("/mcp",), "Manage Model Context Protocol servers", "_cmd_mcp"),
    CommandSpec(
        ("/update", "/upgrade"),
        "Check for and install CLI updates",
        "_cmd_update",
    ),
)


# Flat set used by the slash-completer; the REPL imports this directly.
COMMANDS: Final[frozenset[str]] = frozenset(
    name for spec in COMMAND_SPECS for name in spec.names
)


def get_command_help() -> list[tuple[str, str]]:
    """Return ``(name, help)`` pairs in display order.

    Aliases share their primary command's help line.
    """
    return [
        (
            spec.primary if len(spec.names) == 1 else " · ".join(spec.names),
            spec.help,
        )
        for spec in COMMAND_SPECS
    ]


#: Valid values for /reasoning-effort — matches ModelConfig.reasoning_effort
#: (phoson_llm/schemas/inputs.py), which OpenAI-compatible backends forward
#: as-is (e.g. o1/o3's ``reasoning_effort`` request parameter).
_REASONING_EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})


# ─── Parsing ─────────────────────────────────────────────────────────────────


@dataclass
class Command:
    """Represents a parsed slash command."""

    name: str
    args: str


def parse_command(text: str) -> Command | None:
    """Parse a string input into a Command, if it starts with '/'."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    name = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    return Command(name=name, args=args)


# ─── Handler ─────────────────────────────────────────────────────────────────


class CommandHandler:
    """Handles execution of CLI slash commands.

    The dispatch table is built once per instance from :data:`COMMAND_SPECS`.
    """

    def __init__(self, repl: "PhosonRepl", host: CommandHost | None = None) -> None:
        """Initialize handler with a session host and a presentation host.

        Args:
            repl: Session facade (classic REPL or the TUI adapter).
            host: Presentation adapter. Defaults to the Rich/prompt_toolkit
                host so existing ``CommandHandler(repl)`` call sites keep
                working.
        """
        self.repl = repl
        self.host = host if host is not None else RendererCommandHost(repl)
        self._dispatch: dict[str, CommandHandlerFn] = {}
        for spec in COMMAND_SPECS:
            method = getattr(self.__class__, spec.method, None)
            if method is None:
                raise RuntimeError(
                    f"CommandHandler is missing method {spec.method!r} "
                    f"for command {spec.primary}"
                )
            for name in spec.names:
                self._dispatch[name] = method

    async def handle(self, cmd: Command) -> bool:
        """Execute ``cmd``. Return ``False`` if the REPL should exit."""
        handler = self._dispatch.get(cmd.name)
        if handler is None:
            self.host.print_error(f"Unknown command: {cmd.name}")
            return True
        return await handler(self, cmd)

    # ── Helpers ─────────────────────────────────────────────────────────

    @property
    def _r(self) -> CommandHost:
        """Shortcut to the presentation host (historically the renderer)."""
        return self.host

    def _available_providers(self) -> list[str]:
        return enabled_providers_from_config(self.repl.config)

    async def _pick_and_set_model(
        self,
        *,
        target: str,  # "main" or "subagent"
        explicit: str | None,
    ) -> None:
        """Shared logic for ``/model`` and ``/subagent-model``.

        When ``explicit`` is None opens the picker; when ``"list"`` prints
        the available models; otherwise sets the model directly.
        """
        r = self._r
        current = (
            self.repl.current_model if target == "main" else self.repl.subagent_model
        )

        if explicit == "list":
            models = await list_available_models(self.repl.config)
            if not models:
                r.print_info("No models available.")
                return
            label = "models" if target == "main" else "sub-agent models"
            r.print_info(f"Available {label}:")
            for option in models:
                marker = "*" if option.id == current else " "
                suffix = f" [{option.provider}]" if option.provider else ""
                r.print_info(f" {marker} {option.id}{suffix}")
            return

        chosen: str | None = explicit
        if not chosen:
            models = await list_available_models(self.repl.config)
            if not models:
                r.print_info("No models available.")
                return
            result = await self.host.pick_model(models, current)
            if result.cancelled or not result.model_id:
                r.print_info("Cancelled.")
                return
            chosen = result.model_id

        if target == "main":
            await self.repl.set_model(chosen)
            save_config(self.repl.config, only_fields={"model"})
            r.print_info(f"Model → {self.repl.current_model}  ·  saved")
        else:
            self.repl.subagent_model = chosen
            self.repl.config.subagent_model = chosen
            self.repl.engine.context.extra["default_model"] = chosen
            self.repl.engine.context.extra["main_model"] = (
                self.repl.engine.context.extra.get("main_model")
                or self.repl.config.model
            )
            save_config(self.repl.config, only_fields={"subagent_model"})
            r.print_info(f"Sub-agent model → {chosen}  ·  saved")

    # ── Command implementations ─────────────────────────────────────────

    async def _cmd_exit(self, cmd: Command) -> bool:  # noqa: ARG002
        return False

    async def _cmd_new(self, cmd: Command) -> bool:  # noqa: ARG002
        maybe = self.repl.new_session()
        if inspect.isawaitable(maybe):
            await maybe  # type: ignore[misc]
        self._r.print_info(f"New session  {self.repl.tree.session_id[:8]}")
        return True

    async def _cmd_model(self, cmd: Command) -> bool:
        await self._pick_and_set_model(target="main", explicit=cmd.args or None)
        return True

    async def _cmd_subagent_model(self, cmd: Command) -> bool:
        await self._pick_and_set_model(target="subagent", explicit=cmd.args or None)
        return True

    async def _cmd_reasoning_effort(self, cmd: Command) -> bool:
        r = self._r
        current = self.repl.config.reasoning_effort

        arg = cmd.args.strip().lower()
        if not arg:
            r.print_info(
                f"Reasoning effort: {current or 'off'}"
                "  ·  usage: /reasoning-effort <low|medium|high|off>"
            )
            return True

        if arg in {"off", "none", "default"}:
            chosen = None
        elif arg in _REASONING_EFFORTS:
            chosen = arg
        else:
            r.print_error(
                f"Unknown reasoning effort: {arg!r}  ·  use low, medium, high, or off"
            )
            return True

        self.repl.config.reasoning_effort = chosen
        save_config(self.repl.config, only_fields={"reasoning_effort"})
        r.print_info(f"Reasoning effort → {chosen or 'off'}  ·  saved")
        return True

    async def _cmd_provider(self, cmd: Command) -> bool:
        r = self._r
        providers = self._available_providers()
        if not providers:
            r.print_info("No providers configured. Run /setup first.")
            return True

        if cmd.args == "list":
            r.print_info("Available providers:")
            for provider in providers:
                marker = "*" if provider == self.repl.config.provider else " "
                r.print_info(f" {marker} {provider}")
            return True

        target_provider = cmd.args or None
        if not target_provider:
            result = await self.host.pick_provider(providers, self.repl.config.provider)
            if result.cancelled or not result.provider:
                r.print_info("Cancelled.")
                return True
            target_provider = result.provider

        if target_provider not in providers:
            r.print_error(f"Provider not configured: {target_provider}")
            return True

        try:
            await self.repl.set_provider(target_provider)
        except ValueError as exc:
            r.print_error(str(exc))
            return True

        save_config(self.repl.config, only_fields={"provider", "model"})
        r.print_info(
            "Provider → "
            f"{self.repl.config.provider}  ·  "
            f"Model → {self.repl.current_model}  ·  saved"
        )
        r.print_info("Use /model <name> to pick a model for this provider.")
        return True

    async def _cmd_tree(self, cmd: Command) -> bool:  # noqa: ARG002
        self._r.print_info(self.repl.render_tree_ascii())
        return True

    async def _cmd_label(self, cmd: Command) -> bool:
        if not cmd.args:
            self._r.print_info("Usage:  /label <text>")
            return True
        self.repl.label_current_node(cmd.args)
        self._r.print_info(f"Labelled  “{cmd.args}”")
        return True

    async def _cmd_title(self, cmd: Command) -> bool:
        r = self._r
        title = cmd.args.strip()
        if not title:
            current = await self._current_session_title()
            shown = f"“{current}”" if current else "(untitled)"
            r.print_info(f"Usage:  /title <text>  —  current: {shown}")
            return True
        if len(title) > 80:
            title = title[:80]
        # Persist on the tree and flush to the session_meta record.
        self.repl.tree.title = title
        await self.repl.storage.save(self.repl.tree)
        meta = self.repl.session_metrics.to_meta()
        meta["title"] = title
        await self.repl.storage.save_meta(self.repl.tree.session_id, meta)
        r.print_info(f"Titled  “{title}”")
        return True

    async def _current_session_title(self) -> str | None:
        metas = await self.repl.storage.list_meta()
        for m in metas:
            if str(m.id) == str(self.repl.tree.session_id):
                return m.title
        return None

    async def _cmd_undo(self, cmd: Command) -> bool:  # noqa: ARG002
        ok, info = self.repl.undo_last_turn()
        if ok:
            self._r.print_info(f"\u21a9 Undid last turn (cursor \u2192 {info[:8]})")
        else:
            self._r.print_info(info)
        return True

    async def _cmd_attach(self, cmd: Command) -> bool:
        r = self._r
        if not cmd.args:
            pending = self.repl.attachments.list_pending()
            if not pending:
                r.print_info("No pending attachments. Usage:  /attach <path> [--clear]")
                return True
            r.print_info(f"{len(pending)} attachment(s) pending:")
            for a in pending:
                r.print_info(f"  📎 {a.path}")
            return True

        if cmd.args == "--clear":
            count = len(self.repl.attachments)
            self.repl.attachments.clear()
            r.print_info(f"Cleared {count} attachment(s).")
            return True

        try:
            self.repl.attachments.attach(cmd.args)
        except (FileNotFoundError, ValueError) as exc:
            r.print_error(str(exc))
            return True

        r.print_info(f"Attached  {cmd.args}")
        warning = provider_compat_warning(
            Path(cmd.args).suffix.lower(), self.repl.config.provider
        )
        if warning:
            r.print_info(f"⚠ {warning}")

        return True

    async def _cmd_help(self, cmd: Command) -> bool:  # noqa: ARG002
        self._r.print_help(get_command_help())
        return True

    async def _cmd_setup(self, cmd: Command) -> bool:  # noqa: ARG002
        await self.host.run_setup()
        return True

    async def _cmd_sessions(self, cmd: Command) -> bool:
        r = self._r
        arg = cmd.args.strip()

        # Inline numbered-list flow (#55): `/sessions` prints a compact
        # recent-sessions table; `/sessions load <n>` loads entry n.
        # The modal Float picker remains available via `/sessions pick`.
        if arg.startswith("load"):
            num_part = arg[len("load") :].strip()
            if not num_part.isdigit():
                r.print_info("Usage:  /sessions load <number>  (see /sessions)")
                return True
            sessions = await self.repl.storage.list_meta()
            idx = int(num_part) - 1
            if idx < 0 or idx >= len(sessions):
                r.print_error(f"No session #{num_part}. Run /sessions to list.")
                return True
            ok = await self.repl.load_session(str(sessions[idx].id))
            if ok:
                r.print_info(f"Loaded session  {str(sessions[idx].id)[:8]}")
            return True

        sessions = await self.repl.storage.list_meta()
        if not sessions:
            r.print_info("No saved sessions.")
            return True

        if not arg or arg == "list":
            self._print_session_list(r, sessions)
            if not arg:
                r.print_info("Load: /sessions load <#> · picker: /sessions pick")
            return True

        if arg == "pick":
            return await self._pick_session_modal(r, sessions)

        r.print_info("Usage:  /sessions [list] · /sessions load <#> · /sessions pick")
        return True

    def _print_session_list(self, r: Any, sessions: list[Any]) -> None:
        """Print the inline numbered recent-sessions table (#55)."""
        current = self.repl.tree.session_id
        r.print_info(f"{len(sessions)} saved session(s) — most recent first:")
        for i, s in enumerate(sessions, start=1):
            marker = "▶" if str(s.id) == str(current) else " "
            updated = s.updated_at.strftime("%m-%d %H:%M")
            cost = f"${s.total_cost:.4f}" if s.total_cost else "—"
            title = getattr(s, "title", None) or "(untitled)"
            r.print_info(
                f" {marker} {i:>2}. [{title}]  {updated}  {s.message_count:>3} msgs"
                f"  {cost:>9}"
            )

    async def _pick_session_modal(self, r: Any, sessions: "list[SessionMeta]") -> bool:
        result = await self.host.pick_session(sessions, self.repl.tree.session_id)

        if result.cancelled:
            r.print_info("Cancelled.")
            return True

        if result.session_id is None:
            r.print_error("No session selected.")
            return True

        if result.delete:
            if result.session_id == self.repl.tree.session_id:
                r.print_error(
                    "Cannot delete the current active session. Use /new first."
                )
                return True
            if not await self.host.confirm(
                f"Delete session {result.session_id[:8]}? This cannot be undone."
            ):
                r.print_info("Delete cancelled.")
                return True
            await self.repl.storage.delete(result.session_id)
            r.print_info(
                f"Session {result.session_id[:8]} deleted."
                " Run /sessions again to refresh."
            )
            return True

        ok = await self.repl.load_session(result.session_id)
        if ok:
            r.print_info(f"Loaded session  {result.session_id[:8]}")
        return True

    async def _cmd_delete(self, cmd: Command) -> bool:
        r = self._r
        if not cmd.args:
            r.print_info("Usage:  /delete <session_id>")
            return True
        session_id = cmd.args.strip()
        if session_id == self.repl.tree.session_id:
            r.print_error("Cannot delete the current active session. Use /new first.")
            return True
        if not await self.host.confirm(
            f"Delete session {session_id[:8]}? This cannot be undone."
        ):
            r.print_info("Delete cancelled.")
            return True
        try:
            await self.repl.storage.delete(session_id)
            r.print_info(f"Session {session_id[:8]} deleted.")
        except OSError as exc:
            r.print_error(f"Failed to delete session: {exc}")
        return True

    async def _cmd_env(self, cmd: Command) -> bool:  # noqa: ARG002
        self._r.print_info(
            f"provider={self.repl.config.provider} "
            f"model={self.repl.current_model} "
            f"subagent_model={self.repl.subagent_model} "
            f"reasoning_effort={self.repl.config.reasoning_effort or 'off'} "
            f"cwd={self.repl.config.sessions_dir}"
        )
        return True

    async def _cmd_cost(self, cmd: Command) -> bool:  # noqa: ARG002
        m = self.repl.session_metrics
        self._r.print_info(
            f"cost=${m.total_cost_usd:.5f} credits={m.total_credits:.5f}"
        )
        return True

    async def _cmd_tokens(self, cmd: Command) -> bool:  # noqa: ARG002
        m = self.repl.session_metrics
        self._r.print_info(
            f"tokens={m.total_input_tokens}in/{m.total_output_tokens}out"
        )
        return True

    async def _cmd_steps(self, cmd: Command) -> bool:  # noqa: ARG002
        self._r.print_info(f"steps={self.repl.session_metrics.step_count}")
        return True

    async def _cmd_mcp(self, cmd: Command) -> bool:
        return await _MCPSubcommands(self).dispatch(cmd)

    async def _cmd_update(self, cmd: Command) -> bool:  # noqa: ARG002
        """Check PyPI and install the latest CLI release (asks first)."""
        summary = await perform_self_update(assume_yes=False, confirm=self.host.confirm)
        for line in summary.splitlines():
            self._r.print_info(line)
        return True
