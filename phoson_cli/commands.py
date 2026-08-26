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

import os
import re
import inspect
from typing import TYPE_CHECKING, Any, Final
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Iterable, Awaitable

from prompt_toolkit.document import Document
from prompt_toolkit.completion import (
    Completer,
    Completion,
    CompleteEvent,
    WordCompleter,
    FuzzyCompleter,
)

from phoson_llm.schemas import REASONING_EFFORTS

from .theme import VALID_NAMES, get_theme
from .config import save_config, enabled_providers_from_config
from .updater import perform_self_update
from .installer import run_install_wizard  # noqa: F401 - patched by tests / host
from .attachments import provider_compat_warning
from .command_host import CommandHost, HelpEntries, RendererCommandHost
from .model_picker import pick_model  # noqa: F401 - patched by tests / host
from .theme_picker import (  # noqa: F401 - patched by tests / host
    ThemePickerResult,
    pick_theme,
)
from ._mcp_commands import _MCPSubcommands
from .file_mentions import format_file_size, iter_candidate_paths
from .model_selector import list_available_models
from .provider_picker import pick_provider  # noqa: F401 - patched by tests / host
from .permissions_store import load_policy

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


#: /help sections (IMPROVEMENTS.md C4): each command spec declares the
#: category it renders under. Commands not listed fall into "Other".
HELP_CATEGORIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "Session",
        ("/new", "/tree", "/undo", "/compact", "/resume", "/sessions", "/delete"),
    ),
    ("Model", ("/model", "/provider", "/subagent-model", "/reasoning-effort")),
    ("Info", ("/status", "/env", "/cost", "/tokens", "/steps", "/agents-md")),
    (
        "Config & System",
        (
            "/label",
            "/title",
            "/theme",
            "/keys",
            "/attach",
            "/permissions",
            "/mcp",
            "/setup",
            "/update",
            "/help",
            "/exit",
        ),
    ),
)

#: Spec order is display order inside a category; unknown commands land in
#: "Other" so a forgotten registration is still visible in /help.
_CATEGORY_ORDER: Final[dict[str, int]] = {
    name: idx for idx, (_title, names) in enumerate(HELP_CATEGORIES) for name in names
}


def get_grouped_command_help() -> list[tuple[str, list[tuple[str, str]]]]:
    """Return ``/help`` entries grouped by category, in category order.

    Returns:
        A list of ``(category_title, [(name, help), ...])`` pairs. Specs
        whose primary command (or any alias) is not listed in
        :data:`HELP_CATEGORIES` are collected under an "Other" section.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    for spec in COMMAND_SPECS:
        entry_name = spec.primary if len(spec.names) == 1 else " · ".join(spec.names)
        entry = (entry_name, spec.help)
        category = next(
            (
                title
                for title, names in HELP_CATEGORIES
                if any(n in names for n in spec.names)
            ),
            "Other",
        )
        grouped.setdefault(category, []).append(entry)

    titles = [title for title, _names in HELP_CATEGORIES]
    ordered: list[tuple[str, list[tuple[str, str]]]] = []
    for title in titles + ["Other"]:
        if title in grouped:
            ordered.append((title, grouped.pop(title)))
    # Anything left (unknown categories can't happen today, but stay safe).
    for title, entries in grouped.items():
        ordered.append((title, entries))
    return ordered


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
        "Show or set reasoning effort: low, medium, high, xhigh, max, or off",
        "_cmd_reasoning_effort",
    ),
    CommandSpec(("/tree",), "Show the conversation tree as ASCII", "_cmd_tree"),
    CommandSpec(
        ("/compact",),
        "Compact the conversation (preview + confirm); /compact on|off "
        "toggles auto-compaction",
        "_cmd_compact",
    ),
    CommandSpec(
        ("/resume",),
        "Resume a saved session by id (prefix match works)",
        "_cmd_resume",
    ),
    CommandSpec(
        ("/status",),
        "Show provider, model, session, cost, tokens and permissions",
        "_cmd_status",
    ),
    CommandSpec(
        ("/sessions",), "List, load (#) or pick saved sessions", "_cmd_sessions"
    ),
    CommandSpec(("/delete",), "Delete a session by id", "_cmd_delete"),
    CommandSpec(("/label",), "Label the current node with a short name", "_cmd_label"),
    CommandSpec(
        ("/title",), "Set a human-readable title for this session", "_cmd_title"
    ),
    CommandSpec(
        ("/theme",),
        "Show, pick or set the color theme: dark, light, ansi, no-color",
        "_cmd_theme",
    ),
    CommandSpec(
        ("/keys",),
        "List the key bindings (full-screen TUI; [keys] remaps in config.toml)",
        "_cmd_keys",
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
    CommandSpec(
        ("/permissions", "/perms"),
        "Show or change per-tool permissions: /permissions <tool> <allow|ask|deny>",
        "_cmd_permissions",
    ),
    CommandSpec(
        ("/agents-md",),
        "Show which AGENTS.md/CLAUDE.md memory files are loaded into the prompt",
        "_cmd_agents_md",
    ),
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


#: ``name -> help`` table built from the central COMMAND_SPECS so the
#: completer's meta column stays in sync with /help and the dispatch table.
_CMD_META: Final[dict[str, str]] = {
    name: spec.help for spec in COMMAND_SPECS for name in spec.names
}


class SlashCompleter(Completer):
    """Completes slash commands only when the buffer starts with ``/``.

    Shared by both front ends (classic REPL and full-screen app) so the
    completion list can never drift from ``COMMAND_SPECS``.
    """

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


class PathCompleter(Completer):
    """Completes ``@path`` file mentions in free text (IMPROVEMENTS.md E3).

    The standard ``@``-mention pattern (Cursor / Claude Code): when the text
    before the cursor ends with ``@`` (at the start of the input or right
    after whitespace) the completer offers repo paths, filtered fuzzy by what
    is typed after the ``@``. Selecting a candidate inserts the *path* — the
    completer does no expansion itself; the controller resolves the mention
    into the file's content when the message is sent (see
    :func:`phoson_cli.file_mentions.expand_file_mentions`).

    Shared by both front ends (classic REPL and full-screen app). Candidate
    paths come from :func:`phoson_cli.file_mentions.iter_candidate_paths`,
    which walks the working tree lazily (once, on the first ``@``) and caps
    depth and entry count so a large repo never blocks the input.

    Args:
        cwd: The directory relative mentions resolve against (and the walk
            root). Defaults to the current working directory.
    """

    def __init__(self, cwd: Path | None = None) -> None:
        self._cwd = (cwd or Path.cwd()).resolve()
        self._candidates: list[str] = []

    def _refresh(self) -> None:
        self._candidates = list(iter_candidate_paths(self._cwd))

    def _query_after_mention(self, text: str) -> str | None:
        """The path query after a trailing ``@``, or ``None`` if no mention.

        The mention is the ``@``-word at the end of the text before the
        cursor: a bare ``@`` or ``@`` followed by path-ish characters
        (letters, digits, ``.``, ``_``, ``-``, ``/``). A preceding word
        char, ``.``, ``-`` or ``@`` (e.g. ``foo@bar.com``, ``v1.@x``) means
        it is not a mention.
        """
        m = re.search(r"(?<![\w.\-@])@([A-Za-z0-9._~/-]*)$", text)
        return m.group(1) if m else None

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        query = self._query_after_mention(document.text_before_cursor)
        if query is None:
            return

        # Walk the tree lazily, once, on the first ``@`` — bounded by the
        # depth/entry caps so it never blocks the input. Cached for the
        # completer's lifetime; the root is fixed so there is nothing new
        # to walk for.
        if not self._candidates:
            self._refresh()

        inner = FuzzyCompleter(WordCompleter(self._candidates, sentence=True))
        sub_document = Document(query, len(query))
        for c in inner.get_completions(sub_document, complete_event):
            display_meta = c.display_meta
            if not c.text.endswith("/"):
                try:
                    display_meta = format_file_size((self._cwd / c.text).stat().st_size)
                except OSError:
                    pass
            yield Completion(
                c.text,
                start_position=c.start_position,
                display=c.display,
                display_meta=display_meta,
                style=c.style,
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


#: Valid values for /reasoning-effort — single source of truth is
#: ``phoson_llm.schemas.REASONING_EFFORTS`` (matches
#: ``ModelConfig.reasoning_effort``), which OpenAI-compatible backends
#: forward as-is (e.g. o1/o3's ``reasoning_effort`` request parameter).
_REASONING_EFFORTS: Final[frozenset[str]] = frozenset(REASONING_EFFORTS)


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
                "  ·  usage: /reasoning-effort <low|medium|high|xhigh|max|off>"
            )
            return True

        if arg in {"off", "none", "default"}:
            chosen = None
        elif arg in _REASONING_EFFORTS:
            chosen = arg
        else:
            r.print_error(
                f"Unknown reasoning effort: {arg!r}  ·  use "
                f"{', '.join(REASONING_EFFORTS)}, or off"
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
        from ._views import render_tree_rich

        self._r.print_renderable(
            render_tree_rich(self.repl.tree, self.repl.current_node_id, self.repl.theme)
        )
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

    async def _cmd_theme(self, cmd: Command) -> bool:
        """Show, pick, or set the color theme (IMPROVEMENTS.md E4)."""
        r = self._r
        current = self.repl.theme.name

        arg = cmd.args.strip().lower()
        if not arg:
            # Zero-cost hint for the picker's "detected" marker: only the
            # COLORFGBG env (no tty IO — an OSC 11 probe here would race
            # the app's own input reader for keystrokes).
            from .terminal_theme import parse_colorfgbg

            detected = parse_colorfgbg(os.environ.get("COLORFGBG"))
            result: ThemePickerResult = await self.host.pick_theme(
                current,
                detected_theme=(
                    "light" if detected else ("dark" if detected is False else None)
                ),
            )
            if result.cancelled or not result.theme_name:
                r.print_info("Cancelled.")
                return True
            arg = result.theme_name
        elif arg == "list":
            r.print_info("Available themes:")
            for name in VALID_NAMES:
                marker = "*" if name == current else " "
                r.print_info(f" {marker} {name}")
            return True

        theme = get_theme(arg)
        if theme is None:
            r.print_error(f"Unknown theme: {arg!r}  ·  use {', '.join(VALID_NAMES)}")
            return True

        self.repl.config.theme = theme.name
        save_config(self.repl.config, only_fields={"theme"})
        self.host.apply_theme(theme)
        r.print_info(f"Theme → {theme.name}  ·  saved")
        return True

    async def _cmd_keys(self, cmd: Command) -> bool:  # noqa: ARG002
        """List the effective key bindings (IMPROVEMENTS.md E6).

        The map is TUI-specific (the classic front end has no global
        key map beyond the prompt's own), so in the classic REPL the
        command still shows the *TUI* map plus how to remap keys —
        both front ends share one source of truth.
        """
        r = self._r
        from .fullscreen.keys import listing_for_config

        rows = listing_for_config(self.repl.config)
        width = max(len(action) for action, _ in rows)
        r.print_info("Key bindings (full-screen TUI):")
        for action, keys in rows:
            r.print_info(f"  {action:<{width}}  {keys}")
        r.print_info("Remap from the [keys] section of ~/.phoson/config.toml:")
        r.print_info("  [keys]")
        r.print_info('  toggle_reasoning = "c-x"      # one sequence')
        r.print_info('  line_up = ["s-up", "c-up"]    # list = precedence')
        r.print_info('  submit = ""                   # unbind an action')
        r.print_info(
            "Restart phoson-cli (or start the TUI) to apply; an unparseable "
            "sequence or a key bound to two actions is an error at startup."
        )
        return True

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
        grouped: HelpEntries = get_grouped_command_help()
        self._r.print_help(grouped)
        return True

    async def _cmd_compact(self, cmd: Command) -> bool:
        """Compact the conversation now (IMPROVEMENTS.md C2 + E1).

        Forms:
          ``/compact``                    — preview + confirm, then compact
          ``/compact aggressive``         — preview + confirm, deeper cut
          ``/compact balanced``           — preview + confirm
          ``/compact on|off``             — enable/disable auto-compaction
          ``/compact yes``                — apply the last preview without asking
        """
        r = self._r
        arg = cmd.args.strip()

        # Mode switch: ``on``/``off`` control *automatic* compaction, not a
        # compaction run. ``balanced``/``aggressive`` are reserved for the
        # manual profile below (unambiguous that way).
        if arg in ("on", "off"):
            ok = self.repl.set_compact_mode(arg)
            if not ok:
                r.print_error(
                    "Unknown compact mode — use balanced, aggressive, on or off."
                )
            return True

        if arg and arg not in ("aggressive", "balanced", "yes"):
            r.print_error("Usage: /compact [balanced|aggressive|on|off|yes]")
            return True

        profile = arg if arg in ("aggressive", "balanced") else None

        if self.repl.is_running:
            r.print_warn("A turn is running — press Esc (or Ctrl+C) first.")
            return True

        plan = self.repl.plan_compaction(profile)
        if not plan.ok:
            r.print_info(plan.reason)
            return True

        if arg == "yes":
            confirmed = True  # already previewed; apply directly
        else:
            r.print_info(
                f"Would summarize {plan.summarize_messages} of {plan.total_messages} "
                f"turns (~{plan.estimated_tokens:,} tokens), keeping the last "
                f"{plan.keep_messages}. Profile: {plan.profile}."
            )
            confirmed = await self.host.confirm("Apply this compaction now?")
            if not confirmed:
                r.print_info("Compaction cancelled.")
                return True

        r.print_info("Compacting conversation…")
        try:
            before, after, changed = await self.repl.compact_context(profile)
        except Exception as exc:  # noqa: BLE001
            r.print_error(f"Compaction failed: {exc}")
            return True
        if not changed:
            return True
        saved = before - after
        percent = (saved / before * 100) if before else 0.0
        r.print_info(
            f"Compacted: {before:,} → {after:,} tokens"
            f"  ·  −{saved:,} ({percent:.0f}% smaller)"
        )
        return True

    async def _cmd_status(self, cmd: Command) -> bool:  # noqa: ARG002
        """One consolidated runtime view (IMPROVEMENTS.md C2).

        Replaces the four atomized /env /cost /tokens /steps commands,
        which remain as aliases of their original behavior.
        """
        from importlib.metadata import PackageNotFoundError, version

        try:
            pkg_version = version("phoson-engine-minimal")
        except PackageNotFoundError:  # pragma: no cover - not installed
            pkg_version = "dev"

        r = self._r
        m = self.repl.session_metrics
        policy = load_policy()
        active_levels = [f"{t}:{lvl}" for t, lvl in sorted(policy.levels.items())]
        permissions = ", ".join(active_levels) if active_levels else "all allow"

        mcp_count = 0
        for plugin in getattr(self.repl.engine, "_loaded_plugins", []):
            servers = getattr(plugin, "servers", {})
            if isinstance(servers, dict):
                mcp_count += len(servers)

        window = self.repl._context_window
        used = self.repl._context_tokens
        pct = f" ({used / window * 100:.0f}%)" if window > 0 else ""

        lines = [
            f"version     {pkg_version}",
            f"provider    {self.repl.config.provider}",
            f"model       {self.repl.current_model}",
            f"subagent    {self.repl.subagent_model}",
            f"effort      {self.repl.config.reasoning_effort or 'off'}",
            f"session     {self.repl.tree.session_id[:8]}"
            f"  ·  {self.repl.tree.node_count()} nodes",
            f"cwd         {Path.cwd()}",
            f"steps       {m.step_count}",
            f"tokens      {m.total_input_tokens:,} in / {m.total_output_tokens:,} out",
            f"context     {used:,}/{window:,}{pct}",
            f"cost        ${m.total_cost_usd:.5f}  ·  credits {m.total_credits:.5f}",
            f"mcp         {mcp_count} server(s)",
            f"permissions {permissions}",
        ]
        r.print_info("\n".join(lines))
        return True

    async def _cmd_resume(self, cmd: Command) -> bool:
        """Load a session directly by id — prefix match works (C2)."""
        r = self._r
        query = cmd.args.strip()
        if not query:
            r.print_info("Usage:  /resume <session_id>  (see /sessions)")
            return True

        sessions = await self.repl.storage.list_meta()
        matches = [s for s in sessions if str(s.id).startswith(query)]
        if not matches:
            r.print_error(f"No session matching {query!r}. Run /sessions to list.")
            return True
        if len(matches) > 1:
            r.print_info(f"{len(matches)} sessions match {query!r}:")
            for s in matches[:10]:
                title = getattr(s, "title", None) or "(untitled)"
                r.print_info(f"  {str(s.id)[:8]}  [{title}]")
            r.print_info("Be more specific.")
            return True

        session_id = str(matches[0].id)
        ok = await self.repl.load_session(session_id)
        if ok:
            title = getattr(matches[0], "title", None)
            suffix = f"  ·  [{title}]" if title else ""
            r.print_info(f"Resumed session  {session_id[:8]}{suffix}")
        return True

    async def _cmd_permissions(self, cmd: Command) -> bool:
        """List or change per-tool permission levels (IMPROVEMENTS.md A1).

        ``/permissions``                — list configured levels + patterns
        ``/permissions <tool> <level>`` — set a level in place and persist

        Levels: allow (run freely) · ask (confirm every call) · deny.
        Changes take effect immediately and persist to permissions.json;
        allow-patterns live in ~/.phoson/permissions.json too (edited via
        "[a] always" answers or by hand).
        """
        from .permissions_store import (
            LEVEL_ALLOW,
            VALID_LEVELS,
            set_level,
            load_policy,
            save_policy,
        )  # noqa: I001

        r = self._r
        policy = load_policy()

        if not cmd.args:
            if not policy.levels and not policy.allow_patterns:
                r.print_info(
                    "No permission rules configured — all tools run freely."
                    "\nUsage: /permissions <tool> <allow|ask|deny>"
                    "\nExample: /permissions bash ask"
                )
                return True
            r.print_info("Per-tool permissions:")
            for tool, level in sorted(policy.levels.items()):
                patterns = policy.allow_patterns.get(tool, [])
                suffix = f"  (always allows: {', '.join(patterns)})" if patterns else ""
                r.print_info(f"  {tool}: {level}{suffix}")
            for tool, patterns in sorted(policy.allow_patterns.items()):
                if tool not in policy.levels:
                    r.print_info(f"  {tool}: allow  (patterns: {', '.join(patterns)})")
            r.print_info("Change with: /permissions <tool> <allow|ask|deny>")
            return True

        parts = cmd.args.split()
        if len(parts) != 2 or parts[1] not in VALID_LEVELS:
            r.print_error("Usage: /permissions <tool> <allow|ask|deny>")
            return True
        tool, level = parts
        if not set_level(policy, tool, level):
            r.print_error(f"Invalid level: {level!r} — use allow, ask or deny")
            return True
        save_policy(policy)
        note = ""
        if level == LEVEL_ALLOW:
            note = " (allow is the default — the entry is dropped from the file)"
        r.print_info(f"{tool} → {level} · saved{note}")
        return True

    async def _cmd_agents_md(self, cmd: Command) -> bool:  # noqa: ARG002
        """List the AGENTS.md/CLAUDE.md files injected into the system prompt."""
        from .agents_md import collect_agents_md_files

        r = self._r
        files = collect_agents_md_files()
        if not files:
            r.print_info(
                "No AGENTS.md/CLAUDE.md files loaded."
                " Create an AGENTS.md in the repo root (or ~/.phoson/AGENTS.md)"
                " to give the agent persistent project instructions."
            )
            return True

        total_chars = sum(len(content) for _, content in files)
        r.print_info(
            f"{len(files)} memory file(s) loaded into the system prompt"
            f" (~{total_chars // 4} tokens, capped at"
            " 2000):"
        )
        for path, content in files:
            lines = content.count("\n") + 1
            marker = "*" if path.parent == Path.cwd() else " "
            r.print_info(f" {marker} {path}  ({lines} lines)")
        r.print_info("(* in the working directory · re-read every turn)")
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
