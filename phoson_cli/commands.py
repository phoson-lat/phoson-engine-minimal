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

from typing import TYPE_CHECKING, Final
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from .config import save_config, enabled_providers_from_config
from .installer import run_install_wizard
from .model_picker import pick_model
from .model_selector import list_available_models
from .provider_picker import pick_provider

if TYPE_CHECKING:
    from .repl import PhosonRepl
    from .renderer import Renderer


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
    CommandSpec(("/tree",), "Show the conversation tree as ASCII", "_cmd_tree"),
    CommandSpec(("/sessions",), "List, load or delete saved sessions", "_cmd_sessions"),
    CommandSpec(("/delete",), "Delete a session by id", "_cmd_delete"),
    CommandSpec(("/branch",), "Branch the current node into a new path", "_cmd_branch"),
    CommandSpec(("/label",), "Label the current node with a short name", "_cmd_label"),
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

    def __init__(self, repl: "PhosonRepl") -> None:
        """Initialize handler with a REPL reference.

        Args:
            repl: The PhosonRepl instance to operate on.
        """
        self.repl = repl
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
            self.repl.renderer.print_error(f"Unknown command: {cmd.name}")
            return True
        return await handler(self, cmd)

    # ── Helpers ─────────────────────────────────────────────────────────

    @property
    def _r(self) -> "Renderer":
        """Shortcut to the renderer."""
        return self.repl.renderer

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
            result = await pick_model(models=models, current_model=current)
            if result.cancelled or not result.model_id:
                r.print_info("Cancelled.")
                return
            chosen = result.model_id

        if target == "main":
            self.repl.set_model(chosen)
            save_config(self.repl.config)
            r.print_info(f"Model → {self.repl.current_model}  ·  saved")
        else:
            self.repl.subagent_model = chosen
            self.repl.config.subagent_model = chosen
            self.repl.engine.context.extra["default_model"] = chosen
            save_config(self.repl.config)
            r.print_info(f"Sub-agent model → {chosen}  ·  saved")

    # ── Command implementations ─────────────────────────────────────────

    async def _cmd_exit(self, cmd: Command) -> bool:  # noqa: ARG002
        return False

    async def _cmd_new(self, cmd: Command) -> bool:  # noqa: ARG002
        self.repl.new_session()
        self._r.print_info(f"New session  {self.repl.tree.session_id[:8]}")
        return True

    async def _cmd_model(self, cmd: Command) -> bool:
        await self._pick_and_set_model(target="main", explicit=cmd.args or None)
        return True

    async def _cmd_subagent_model(self, cmd: Command) -> bool:
        await self._pick_and_set_model(target="subagent", explicit=cmd.args or None)
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
            result = await pick_provider(
                providers=providers,
                current_provider=self.repl.config.provider,
            )
            if result.cancelled or not result.provider:
                r.print_info("Cancelled.")
                return True
            target_provider = result.provider

        if target_provider not in providers:
            r.print_error(f"Provider not configured: {target_provider}")
            return True

        try:
            self.repl.set_provider(target_provider)
        except ValueError as exc:
            r.print_error(str(exc))
            return True

        models = await list_available_models(self.repl.config)
        if not models:
            save_config(self.repl.config)
            r.print_info(f"Provider → {self.repl.config.provider}  ·  saved")
            r.print_info("No models available for the selected provider.")
            return True

        model_result = await pick_model(
            models=models,
            current_model=self.repl.current_model,
        )
        if model_result.cancelled or not model_result.model_id:
            save_config(self.repl.config)
            r.print_info(f"Provider → {self.repl.config.provider}  ·  saved")
            r.print_info("Model selection cancelled; kept current model.")
            return True

        self.repl.set_model(model_result.model_id)
        save_config(self.repl.config)
        r.print_info(
            "Provider → "
            f"{self.repl.config.provider}  ·  "
            f"Model → {self.repl.current_model}  ·  saved"
        )
        return True

    async def _cmd_tree(self, cmd: Command) -> bool:  # noqa: ARG002
        self._r.print_info(self.repl.render_tree_ascii())
        return True

    async def _cmd_branch(self, cmd: Command) -> bool:  # noqa: ARG002
        self.repl.branch_session()
        node = (self.repl.current_node_id or "")[:8]
        self._r.print_info(f"Branched from  {node}")
        return True

    async def _cmd_label(self, cmd: Command) -> bool:
        if not cmd.args:
            self._r.print_info("Usage:  /label <text>")
            return True
        self.repl.label_current_node(cmd.args)
        self._r.print_info(f"Labelled  \u201c{cmd.args}\u201d")
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
            r.print_info(f"Attached  {cmd.args}")
        except FileNotFoundError as exc:
            r.print_error(str(exc))
        except ValueError as exc:
            r.print_error(str(exc))

        return True

    async def _cmd_help(self, cmd: Command) -> bool:  # noqa: ARG002
        self._r.print_help(get_command_help())
        return True

    async def _cmd_setup(self, cmd: Command) -> bool:  # noqa: ARG002
        self.repl.config = await run_install_wizard(self.repl.config)
        self.repl.set_model(self.repl.config.model)
        self._r.print_info("Setup completed.")
        return True

    async def _cmd_sessions(self, cmd: Command) -> bool:  # noqa: ARG002
        r = self._r
        sessions = await self.repl.storage.list_meta()
        if not sessions:
            r.print_info("No saved sessions.")
            return True

        from phoson_cli.session_picker import pick_session

        result = await pick_session(
            sessions=sessions,
            current_id=self.repl.tree.session_id,
            page_size=15,
        )

        if result.cancelled:
            r.print_info("Cancelled.")
            return True

        if result.delete:
            if result.session_id == self.repl.tree.session_id:
                r.print_error(
                    "Cannot delete the current active session. Use /new first."
                )
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
            r.print_error(
                "Cannot delete the current active session. Use /new first."
            )
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


# ─── /mcp subcommands ────────────────────────────────────────────────────────


class _MCPSubcommands:
    """Dispatcher for the ``/mcp <subcommand>`` family.

    Splitting the MCP commands out keeps :class:`CommandHandler` focused
    on top-level dispatch and gives the MCP family its own small,
    inspectable structure mirroring the main one.
    """

    def __init__(self, parent: CommandHandler) -> None:
        self._parent = parent

    @property
    def repl(self) -> "PhosonRepl":
        return self._parent.repl

    @property
    def r(self) -> "Renderer":
        return self.repl.renderer

    async def dispatch(self, cmd: Command) -> bool:
        args = cmd.args.strip()

        # Bare `/mcp` shows status, like `/mcp status`.
        if args == "" or args == "status":
            return await self._status()
        if args == "init":
            return await self._init()
        if args == "enable":
            return await self._enable()
        if args == "disable":
            return await self._disable()
        if args == "help":
            return await self._help()
        if args.startswith("config "):
            return await self._set_config(args.removeprefix("config ").strip())

        self.r.print_error(f"Unknown /mcp command: {args}")
        self.r.print_info("Use '/mcp help' for available commands")
        return True

    async def _init(self) -> bool:
        import json
        from pathlib import Path

        config_file = self.repl.config.mcp_config_file

        if config_file.exists():
            self.r.print_warn(f"Config file already exists: {config_file}")
            self.r.print_info("Use '/mcp config <path>' to use a different file")
            return True

        config_file.parent.mkdir(parents=True, exist_ok=True)
        example_config = {
            "mcpServers": {
                "filesystem": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": [
                        "-y",
                        "@modelcontextprotocol/server-filesystem",
                        str(Path.home()),
                    ],
                    "env": {},
                },
                "memory": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-memory"],
                    "env": {},
                },
            }
        }

        with open(config_file, "w") as f:
            json.dump(example_config, f, indent=2)

        self.r.print_info(f"✅ Created MCP config: {config_file}")
        self.r.print_info("Configured servers:")
        self.r.print_info("  • filesystem (STDIO) - Access to home directory")
        self.r.print_info("  • memory (STDIO) - Knowledge storage")
        self.r.print_info("")
        self.r.print_info("Next steps:")
        self.r.print_info("  1. Edit the file to add your servers")
        self.r.print_info("  2. Run: /mcp enable")
        self.r.print_info("  3. Run: /mcp status")
        return True

    async def _status(self) -> bool:
        cfg = self.repl.config
        status = "enabled" if cfg.enable_mcp else "disabled"
        self.r.print_info(f"MCP: {status}")

        if not cfg.enable_mcp:
            self.r.print_info("Use '/mcp enable' to activate MCP support")
            return True

        self.r.print_info(f"Config file: {cfg.mcp_config_file}")
        servers_info, tool_prefixes = self._collect_mcp_runtime()

        if servers_info:
            self.r.print_info(f"Configured {len(servers_info)} MCP server(s):")
            for server_name, transport, target in servers_info:
                self.r.print_info(f"  • {server_name} [{transport}] → {target}")

        mcp_tools = (
            [
                t
                for t in self.repl.engine.tools
                if any(t.name.startswith(prefix) for prefix in tool_prefixes)
            ]
            if tool_prefixes
            else []
        )
        if mcp_tools:
            self.r.print_info(f"Loaded {len(mcp_tools)} MCP tool(s):")
            for tool in mcp_tools:
                self.r.print_info(f"  • {tool.name}")
        else:
            self.r.print_info(
                "No MCP tools loaded (check config file / discovery mode)"
            )
        return True

    def _collect_mcp_runtime(
        self,
    ) -> tuple[list[tuple[str, str, str]], set[str]]:
        """Inspect loaded plugins and return (servers_info, tool_prefixes)."""
        servers_info: list[tuple[str, str, str]] = []
        tool_prefixes: set[str] = set()

        for plugin in getattr(self.repl.engine, "_loaded_plugins", []):
            if getattr(plugin, "name", "") != "phoson-plugin-mcp":
                continue
            prefix = str(getattr(plugin, "tool_name_prefix", "mcp"))
            tool_prefixes.add(f"{prefix}_")
            servers = getattr(plugin, "servers", {})
            for server_name, server_cfg in servers.items():
                transport = str(server_cfg.get("transport", "stdio"))
                if transport in {"sse", "http", "streamable_http"}:
                    target = str(server_cfg.get("url"))
                else:
                    target = " ".join(
                        [
                            str(server_cfg.get("command", "")),
                            *[str(a) for a in server_cfg.get("args", [])],
                        ]
                    ).strip()
                servers_info.append((server_name, transport, target))

        return servers_info, tool_prefixes

    async def _enable(self) -> bool:
        if self.repl.config.enable_mcp:
            self.r.print_info("MCP is already enabled")
            return True

        self.repl.config.enable_mcp = True
        save_config(self.repl.config)
        self.repl.set_model(self.repl.current_model)

        self.r.print_info("MCP enabled  ·  saved")
        self.r.print_info(f"Config file: {self.repl.config.mcp_config_file}")
        self.r.print_info("Restart or run '/mcp status' to see loaded tools")
        return True

    async def _disable(self) -> bool:
        if not self.repl.config.enable_mcp:
            self.r.print_info("MCP is already disabled")
            return True

        self.repl.config.enable_mcp = False
        save_config(self.repl.config)
        self.repl.set_model(self.repl.current_model)

        self.r.print_info("MCP disabled  ·  saved")
        return True

    async def _set_config(self, path: str) -> bool:
        from pathlib import Path

        if not path:
            self.r.print_error("Usage: /mcp config <path>")
            return True

        self.repl.config.mcp_config_file = Path(path)
        save_config(self.repl.config)

        if self.repl.config.enable_mcp:
            self.repl.set_model(self.repl.current_model)

        self.r.print_info(f"MCP config file → {path}  ·  saved")
        return True

    async def _help(self) -> bool:
        lines = [
            "MCP (Model Context Protocol) commands:",
            "  /mcp init            Create example config file",
            "  /mcp status          Show MCP status and loaded tools",
            "  /mcp enable          Enable MCP support",
            "  /mcp disable         Disable MCP support",
            "  /mcp config <path>   Set MCP config file path",
            "  /mcp help            Show this help",
            "",
            f"Default config location: {self.repl.config.mcp_config_file}",
            "",
            "Example phoson-mcp.json:",
            "  {",
            '    "mcpServers": {',
            '      "filesystem": {',
            '        "command": "npx",',
            '        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]',
            "      }",
            "    }",
            "  }",
        ]
        for line in lines:
            self.r.print_info(line)
        return True
