"""MCP subcommand dispatcher for the ``/mcp`` command family."""

import os
import re
import json
import shutil
from typing import TYPE_CHECKING, Any
from pathlib import Path

from .config import save_config

if TYPE_CHECKING:
    from .repl import PhosonRepl
    from .commands import Command, CommandHandler
    from .command_host import CommandHost


def _safe_name_part(value: str) -> str:
    """Mirror of ``MCPPlugin._safe_tool_name_part`` (avoid importing the
    plugin here so this module stays dependency-light and testable)."""
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return normalized or "tool"


def toggle_mcp_config(
    config_path: Path,
    server: str,
    tool: str | None = None,
    *,
    tool_prefix: str = "mcp",
) -> tuple[str, bool]:
    """Flip the ``enabled`` flag of an MCP server (I-100).

    Args:
        config_path: path to the ``mcps.json`` file.
        server: server name as it appears in the JSON.
        tool: optional tool name to toggle. Accepts either the remote name
            (``read_file``) or the local prefixed name
            (``mcp_filesystem_read_file``); it's stored keyed by remote
            name in ``servers[server]["tools"]``.
        tool_prefix: local prefix used to resolve local tool names.

    Returns:
        ``(target, new_state)`` where ``target`` is the human-readable
        thing that was toggled and ``new_state`` is its value after the
        flip.

    Raises:
        ValueError: if the config is invalid JSON, has no ``mcpServers``
            map, or the server is unknown.
    """
    if not config_path.exists():
        raise ValueError(f"MCP config file not found: {config_path}")
    try:
        data = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}") from e

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError(f"No 'mcpServers' map in {config_path}")
    if server not in servers:
        known = ", ".join(sorted(servers)) or "(none)"
        raise ValueError(f"Unknown MCP server '{server}'. Configured: {known}")

    if tool is None:
        server_cfg = servers[server]
        if not isinstance(server_cfg, dict):
            raise ValueError(f"Server '{server}' config is not an object")
        new_state = not bool(server_cfg.get("enabled", True))
        server_cfg["enabled"] = new_state
        target = server
    else:
        remote_tool = tool
        local_prefix = f"{tool_prefix}_{_safe_name_part(server)}_"
        if tool.startswith(local_prefix):
            remote_tool = _resolve_remote_tool_name(local_prefix, tool, servers[server])
        server_cfg = servers[server]
        if not isinstance(server_cfg, dict):
            raise ValueError(f"Server '{server}' config is not an object")
        tools_map = server_cfg.get("tools")
        if not isinstance(tools_map, dict):
            tools_map = {}
            server_cfg["tools"] = tools_map
        new_state = not bool(tools_map.get(remote_tool, True))
        tools_map[remote_tool] = new_state
        target = (
            f"{tool_prefix}_{_safe_name_part(server)}_{_safe_name_part(remote_tool)}"
        )

    backup_path = config_path.parent / f"{config_path.name}.bak"
    try:
        if config_path.exists():
            shutil.copy2(config_path, backup_path)
            os.chmod(backup_path, 0o600)
    except OSError:  # pragma: no cover - best-effort safety net
        pass
    config_path.write_text(json.dumps(data, indent=2))
    # F-37: mcps.json can hold secrets — enforce owner-only perms after the
    # rewrite so a pre-existing 0o644/0o666 is never left world-readable.
    try:
        os.chmod(config_path, 0o600)
    except OSError:  # pragma: no cover - best-effort
        pass
    return target, new_state


def _resolve_remote_tool_name(
    local_prefix: str, local_name: str, server_cfg: Any
) -> str:
    """Best-effort local → remote tool name resolution.

    The local name is ``{prefix}_{safe_server}_{safe_remote}``. Inverting
    ``_safe_name_part`` is lossy, so when the server config carries a
    ``tools`` map we try an exact key whose safe form matches; otherwise
    the raw suffix is returned (which round-trips for normal tool names).
    """
    suffix = local_name.removeprefix(local_prefix)
    tools_map = server_cfg.get("tools") if isinstance(server_cfg, dict) else None
    if isinstance(tools_map, dict):
        for remote in tools_map:
            if _safe_name_part(str(remote)) == suffix:
                return str(remote)
    return suffix


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
        if args == "toggle" or args.startswith("toggle "):
            return await self._toggle(args.removeprefix("toggle").strip())
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
            for server_name, transport, target, enabled in servers_info:
                state = "" if enabled else "  (disabled)"
                self.r.print_info(f"  • {server_name} [{transport}] → {target}{state}")
                for disabled_tool in self._disabled_tools(server_name):
                    self.r.print_info(f"      - {disabled_tool}  (disabled)")

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
    ) -> tuple[list[tuple[str, str, str, bool]], set[str]]:
        """Inspect loaded plugins and return (servers_info, tool_prefixes)."""
        servers_info: list[tuple[str, str, str, bool]] = []
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
                enabled = bool(server_cfg.get("enabled", True))
                servers_info.append((server_name, transport, target, enabled))

        return servers_info, tool_prefixes

    def _disabled_tools(self, server_name: str) -> list[str]:
        """Names of tools explicitly turned off for ``server_name``."""
        for plugin in getattr(self.repl.engine, "_loaded_plugins", []):
            if getattr(plugin, "name", "") != "phoson-plugin-mcp":
                continue
            prefix = str(getattr(plugin, "tool_name_prefix", "mcp"))
            servers = getattr(plugin, "servers", {})
            server_cfg = servers.get(server_name) or {}
            tools_map = server_cfg.get("tools") or {}
            return [
                f"{prefix}_{_safe_name_part(server_name)}_{_safe_name_part(name)}"
                for name, enabled in tools_map.items()
                if not enabled
            ]
        return []

    async def _toggle(self, rest: str) -> bool:
        if not rest:
            self.r.print_error("Usage: /mcp toggle <server> [tool]")
            return True

        parts = rest.split(None, 1)
        server = parts[0]
        tool = parts[1].strip() if len(parts) > 1 else None

        prefix = "mcp"
        for plugin in getattr(self.repl.engine, "_loaded_plugins", []):
            if getattr(plugin, "name", "") == "phoson-plugin-mcp":
                prefix = str(getattr(plugin, "tool_name_prefix", "mcp"))
                break

        config_file = self.repl.config.mcp_config_file
        try:
            target, new_state = toggle_mcp_config(
                config_file, server, tool=tool, tool_prefix=prefix
            )
        except ValueError as e:
            self.r.print_error(str(e))
            return True

        mark = "✅" if new_state else "❌"
        state = "enabled" if new_state else "disabled"
        self.r.print_info(f"{mark} {target} → {state}  ·  saved")

        if self.repl.config.enable_mcp:
            await self.repl.set_model(self.repl.current_model)
        else:
            self.r.print_warn(
                "MCP is globally disabled; the change is saved but will not "
                "apply until '/mcp enable'."
            )
        return True

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

        self.repl.config.mcp_config_file = Path(path).expanduser()
        save_config(self.repl.config, only_fields={"mcp_config_file"})

        if self.repl.config.enable_mcp:
            await self.repl.set_model(self.repl.current_model)

        self.r.print_info(
            f"MCP config file → {self.repl.config.mcp_config_file}  ·  saved"
        )
        return True

    async def _help(self) -> bool:
        lines = [
            "MCP (Model Context Protocol) commands:",
            "  /mcp init            Create example config file",
            "  /mcp status          Show MCP status and loaded tools",
            "  /mcp enable          Enable MCP support",
            "  /mcp disable         Disable MCP support",
            "  /mcp config <path>   Set MCP config file path",
            "  /mcp toggle <server> Toggle a whole server on/off",
            "  /mcp toggle <server> <tool>  Toggle one tool on/off",
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
