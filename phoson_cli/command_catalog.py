"""Slash-command catalog: specs, plugin registration and parsing (UI-free).

:data:`COMMAND_SPECS` is the single source of truth for native command
names, aliases and help strings; ``/help``, the slash completers and the
:class:`~phoson_cli.commands.CommandHandler` dispatch table all read it.
:func:`build_command_catalog` validates plugin-contributed commands
against it once per engine build.

Kept apart from :mod:`phoson_cli.commands` (handlers + prompt_toolkit
completers) so the session runtime can build the catalog without the TUI
stack.
"""

import inspect
from typing import Final
from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from phoson_agent import Plugin, CliCommandSpec, CliCommandContext, CliCommandInvocation

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


PluginCommandHandlerFn = Callable[
    [CliCommandInvocation, CliCommandContext], Awaitable[bool]
]


@dataclass(frozen=True)
class RegisteredPluginCommand:
    """A validated plugin command bound to its loaded plugin instance."""

    plugin_name: str
    plugin: Plugin
    spec: CliCommandSpec
    handler: PluginCommandHandlerFn


@dataclass(frozen=True)
class CommandCatalog:
    """Native and plugin commands available to one CLI session.

    ``version`` changes whenever the controller rebuilds the agent runtime;
    handlers/completers can safely refresh without retaining a closed plugin
    instance. Native commands always appear before plugin commands.
    """

    specs: tuple[CommandSpec | CliCommandSpec, ...]
    plugin_commands: dict[str, RegisteredPluginCommand]
    version: int = 0


#: /help sections (IMPROVEMENTS.md C4): each command spec declares the
#: category it renders under. Commands not listed fall into "Other".
HELP_CATEGORIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "Session",
        ("/new", "/tree", "/undo", "/compact", "/resume", "/sessions", "/delete"),
    ),
    ("Model", ("/model", "/provider", "/subagent-model", "/reasoning-effort")),
    (
        "Info",
        (
            "/status",
            "/about",
            "/env",
            "/cost",
            "/tokens",
            "/steps",
            "/agents-md",
            "/skills",
        ),
    ),
    (
        "Config & System",
        (
            "/label",
            "/title",
            "/theme",
            "/keys",
            "/attach",
            "/permissions",
            "/details",
            "/notify",
            "/warnings",
            "/mcp",
            "/auth",
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


def get_grouped_command_help(
    specs: Sequence[CommandSpec | CliCommandSpec] | None = None,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return ``/help`` entries grouped by category, in category order.

    Returns:
        A list of ``(category_title, [(name, help), ...])`` pairs. Specs
        whose primary command (or any alias) is not listed in
        :data:`HELP_CATEGORIES` are collected under an "Other" section.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    for spec in specs or COMMAND_SPECS:
        entry_name = spec.primary if len(spec.names) == 1 else " · ".join(spec.names)
        entry = (entry_name, spec.help)
        category = (
            spec.category
            if isinstance(spec, CliCommandSpec)
            else next(
                (
                    title
                    for title, names in HELP_CATEGORIES
                    if any(n in names for n in spec.names)
                ),
                "Other",
            )
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
    CommandSpec(
        ("/model",),
        "Pick or set the active model (all configured providers)",
        "_cmd_model",
    ),
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
        ("/about",),
        "Show the Phoson wordmark and about info",
        "_cmd_about",
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
        ("/details", "/tool-cards"),
        "Toggle tool-card bodies (diffs, write summaries, bash output)",
        "_cmd_details",
    ),
    CommandSpec(
        ("/notify",),
        "Notify the terminal when a run finishes: /notify <bell|desktop|off>",
        "_cmd_notify",
    ),
    CommandSpec(
        ("/warnings",),
        "Show or set CLI warning notices: /warnings <on|off>",
        "_cmd_warnings",
    ),
    CommandSpec(
        ("/agents-md",),
        "Show which AGENTS.md/CLAUDE.md memory files are loaded into the prompt",
        "_cmd_agents_md",
    ),
    CommandSpec(
        ("/skills",),
        "List available skills, or show one's instructions: /skills <name>",
        "_cmd_skills",
    ),
    CommandSpec(("/env",), "Show provider, model and session info", "_cmd_env"),
    CommandSpec(("/cost",), "Show running cost in USD/credits", "_cmd_cost"),
    CommandSpec(("/tokens",), "Show running input/output token totals", "_cmd_tokens"),
    CommandSpec(("/steps",), "Show the number of agent steps so far", "_cmd_steps"),
    CommandSpec(
        ("/auth",),
        "Sign in/out of subscription providers (ChatGPT, Google Code Assist)",
        "_cmd_auth",
    ),
    CommandSpec(("/setup",), "Run the initial setup wizard again", "_cmd_setup"),
    CommandSpec(("/mcp",), "Manage Model Context Protocol servers", "_cmd_mcp"),
    CommandSpec(
        ("/update", "/upgrade"),
        "Check for and install CLI updates",
        "_cmd_update",
    ),
)


#: Slash commands that stay available while an agent turn is in flight.
#:
#: They are local UI/config actions or read-only info: none of them creates,
#: loads, compacts or deletes a session, switches the model/provider, or
#: otherwise competes with the running turn for the controller. Anything not
#: listed here — including every plugin command, which is unknown by default —
#: is refused mid-run, and a plain message is queued instead.
RUN_SAFE_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        # Local UI / config
        "/theme",
        "/warnings",
        "/notify",
        "/details",
        "/tool-cards",
        "/keys",
        "/help",
        "/permissions",
        "/perms",
        "/reasoning-effort",
        "/effort",
        "/attach",
        "/attachments",
        # Read-only info
        "/status",
        "/about",
        "/env",
        "/cost",
        "/tokens",
        "/steps",
        "/tree",
        "/agents-md",
        "/skills",
    }
)


def is_run_safe_command(name: str) -> bool:
    """Whether the slash command ``name`` may run while a turn is in flight.

    ``name`` is matched verbatim, so every alias of a safe command must be
    listed in :data:`RUN_SAFE_COMMANDS` too.
    """
    return name in RUN_SAFE_COMMANDS


def build_command_catalog(
    plugins: Sequence[Plugin], *, version: int = 0
) -> CommandCatalog:
    """Build and validate the command catalog for one loaded plugin set.

    Native names are permanently reserved. Plugin command aliases must be
    unique globally; no "last plugin wins" policy is allowed because it makes
    a user's active commands depend on installation order.
    """
    reserved = {name for spec in COMMAND_SPECS for name in spec.names}
    seen = set(reserved)
    specs: list[CommandSpec | CliCommandSpec] = list(COMMAND_SPECS)
    registered: dict[str, RegisteredPluginCommand] = {}

    for plugin in plugins:
        try:
            plugin_specs = plugin.get_commands()
        except Exception as exc:
            raise ValueError(
                f"Plugin {plugin.name!r} failed while listing CLI commands: {exc}"
            ) from exc
        for spec in plugin_specs:
            if not isinstance(spec, CliCommandSpec):
                raise ValueError(
                    f"Plugin {plugin.name!r} returned {type(spec).__name__} "
                    "instead of CliCommandSpec"
                )
            if not spec.names or any(
                not name.startswith("/") or " " in name or name == "/"
                for name in spec.names
            ):
                raise ValueError(
                    f"Plugin {plugin.name!r} command names must be non-empty "
                    "slash commands without spaces"
                )
            if len(set(spec.names)) != len(spec.names):
                raise ValueError(
                    f"Plugin {plugin.name!r} repeats an alias in {spec.names!r}"
                )
            collision = next((name for name in spec.names if name in seen), None)
            if collision is not None:
                owner = (
                    "a native command" if collision in reserved else "another plugin"
                )
                raise ValueError(
                    f"Plugin {plugin.name!r} command {collision!r} "
                    f"conflicts with {owner}"
                )
            handler = getattr(plugin, spec.handler, None)
            if (
                handler is None
                or not callable(handler)
                or not inspect.iscoroutinefunction(handler)
            ):
                raise ValueError(
                    f"Plugin {plugin.name!r} handler {spec.handler!r} "
                    "must be an async method"
                )
            command = RegisteredPluginCommand(
                plugin_name=plugin.name,
                plugin=plugin,
                spec=spec,
                handler=handler,
            )
            for name in spec.names:
                seen.add(name)
                registered[name] = command
            specs.append(spec)

    return CommandCatalog(tuple(specs), registered, version)


# Flat set used by the legacy slash-completer API. A per-session catalog is
# used when a controller has loaded community plugins.
COMMANDS: Final[frozenset[str]] = frozenset(
    name for spec in COMMAND_SPECS for name in spec.names
)


#: ``name -> help`` table built from the central COMMAND_SPECS so the
#: completer's meta column stays in sync with /help and the dispatch table.
_CMD_META: Final[dict[str, str]] = {
    name: spec.help for spec in COMMAND_SPECS for name in spec.names
}


def get_command_help(
    specs: Sequence[CommandSpec | CliCommandSpec] | None = None,
) -> list[tuple[str, str]]:
    """Return ``(name, help)`` pairs in display order.

    Aliases share their primary command's help line.
    """
    return [
        (
            spec.primary if len(spec.names) == 1 else " · ".join(spec.names),
            spec.help,
        )
        for spec in specs or COMMAND_SPECS
    ]


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
