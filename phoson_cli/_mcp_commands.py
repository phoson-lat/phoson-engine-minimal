"""MCP subcommand dispatcher for the ``/mcp`` command family."""

from typing import TYPE_CHECKING

from .config import save_config

if TYPE_CHECKING:
    from .repl import PhosonRepl
    from .commands import Command, CommandHandler
    from .command_host import CommandHost


class _MCPSubcommands:
    """Dispatcher for the ``/mcp <subcommand>`` family.

    Splitting the MCP commands out keeps :class:`CommandHandler` focused
    on top-level dispatch and gives the MCP family its own small,
    inspectable structure mirroring the main one.
    """

    def __init__(self, parent: "CommandHandler") -> None:
        self._parent = parent

    @property
    def repl(self) -> "PhosonRepl":
        return self._parent.repl

    @property
    def r(self) -> "CommandHost":
        return self._parent.host

    async def dispatch(self, cmd: "Command") -> bool:
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
        save_config(self.repl.config, only_fields={"enable_mcp"})
        await self.repl.set_model(self.repl.current_model)

        self.r.print_info("MCP enabled  ·  saved")
        self.r.print_info(f"Config file: {self.repl.config.mcp_config_file}")
        self.r.print_info("Restart or run '/mcp status' to see loaded tools")
        return True

    async def _disable(self) -> bool:
        if not self.repl.config.enable_mcp:
            self.r.print_info("MCP is already disabled")
            return True

        self.repl.config.enable_mcp = False
        save_config(self.repl.config, only_fields={"enable_mcp"})
        await self.repl.set_model(self.repl.current_model)

        self.r.print_info("MCP disabled  ·  saved")
        return True

    async def _set_config(self, path: str) -> bool:
        from pathlib import Path

        if not path:
            self.r.print_error("Usage: /mcp config <path>")
            return True

        self.repl.config.mcp_config_file = Path(path)
        save_config(self.repl.config, only_fields={"mcp_config_file"})

        if self.repl.config.enable_mcp:
            await self.repl.set_model(self.repl.current_model)

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
