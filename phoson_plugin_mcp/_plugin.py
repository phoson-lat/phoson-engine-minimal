"""
MCP Plugin implementation.
"""

import re
import json
import asyncio
from typing import Any
from pathlib import Path
from contextlib import AsyncExitStack

from phoson_agent import Plugin, AgentTool

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class MCPPlugin(Plugin):
    """
    Plugin for integrating Model Context Protocol (MCP) servers.

    Loads MCP server configurations from phoson-mcp.json and exposes
    their tools to the agent.

    Configuration file format (phoson-mcp.json):

    STDIO transport (default):
    {
        "mcpServers": {
            "server-name": {
                "transport": "stdio",
                "command": "node",
                "args": ["path/to/server.js"],
                "env": {
                    "API_KEY": "value"
                },
                "enabled": true,
                "tools": {
                    "some_tool": true,
                    "other_tool": false
                }
            }
        }
    }

    Per-server toggles (I-100; both optional, default = everything on):

    - ``"enabled": false`` disables the whole server: none of its tools are
      discovered or exposed to the model.
    - ``"tools": {"<remote_tool>": false}`` disables a single remote tool
      (keyed by the remote name, not the local ``mcp_<server>_<tool>``
      name). A missing map or entry means enabled.

    SSE transport:
    {
        "mcpServers": {
            "server-name": {
                "transport": "sse",
                "url": "http://localhost:3000/sse",
                "headers": {
                    "Authorization": "Bearer token"
                }
            }
        }
    }

    HTTP transport:
    {
        "mcpServers": {
            "server-name": {
                "transport": "http",
                "url": "http://localhost:3000/mcp",
                "headers": {
                    "Authorization": "Bearer token"
                }
            }
        }
    }
    """

    def __init__(self):
        self.config_file: Path = Path("phoson-mcp.json")
        self.servers: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, ClientSession] = {}
        self.tools_cache: list[AgentTool] = []
        self.tool_name_prefix: str = "mcp"
        self._initialized = False
        # Session pooling: one live connection per server, reused across
        # tool calls instead of reconnecting (and re-spawning stdio
        # subprocesses) on every single call.
        self._exit_stack = AsyncExitStack()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._server_tool_lists: dict[str, list[Any]] = {}

    @property
    def name(self) -> str:
        return "phoson-plugin-mcp"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Integrates Model Context Protocol (MCP) servers with Phoson Agent"

    def configure(self, config: dict[str, Any]) -> None:
        """Configure the MCP plugin."""
        if "config_file" in config:
            self.config_file = Path(config["config_file"])

        if "tool_name_prefix" in config:
            self.tool_name_prefix = str(config["tool_name_prefix"])

        # Allow inline server configuration
        if "servers" in config:
            self.servers = config["servers"]

    def initialize(self) -> None:
        """Initialize MCP servers from configuration file."""
        if not MCP_AVAILABLE:
            raise ImportError(
                "MCP package not installed. Install with: pip install mcp"
            )

        # Load configuration from file if it exists
        if self.config_file.exists():
            try:
                with open(self.config_file) as f:
                    config_data = json.load(f)

                # Support both formats: {"mcpServers": {...}} and {"servers": {...}}
                if "mcpServers" in config_data:
                    self.servers.update(config_data["mcpServers"])
                elif "servers" in config_data:
                    self.servers.update(config_data["servers"])
                else:
                    self.servers.update(config_data)

            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in {self.config_file}: {e}")
            except Exception as e:
                raise RuntimeError(f"Failed to load MCP config: {e}")

        if not self.servers:
            # No servers configured, that's okay
            return

        # Initialize tools from servers (eager if possible, deferred in CLI)
        self._load_tools_from_servers()

        # If discovery was deferred, register lightweight proxy tools so the
        # model can still call MCP servers by name.
        if not self.tools_cache:
            for server_name in self.servers.keys():
                if self.is_server_enabled(server_name):
                    self.tools_cache.extend(self._create_proxy_tools(server_name))

        self._initialized = True

    def _load_tools_from_servers(self) -> None:
        """Load and discover tools from all configured MCP servers.

        If we are inside an already-running event loop (e.g. the CLI), run
        discovery in a temporary thread so initialization can still expose
        concrete MCP tools synchronously.
        """
        import asyncio

        try:
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False

        for server_name, server_config in self.servers.items():
            if not self.is_server_enabled(server_name):
                continue
            try:
                discovered_tools = self._discover_tools_blocking(
                    server_name,
                    server_config,
                    use_thread=running_loop,
                )
                self.tools_cache.extend(discovered_tools)
            except Exception as e:
                print(
                    "Warning: Failed to discover tools from MCP server "
                    f"'{server_name}': {e}"
                )

    def _discover_tools_blocking(
        self,
        server_name: str,
        server_config: dict[str, Any],
        *,
        use_thread: bool,
    ) -> list[AgentTool]:
        """Synchronously discover tools, even when another loop is running."""
        import asyncio
        import threading

        if not use_thread:
            return asyncio.run(self._discover_server_tools(server_name, server_config))

        result: list[list[AgentTool]] = []
        errors: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(
                    asyncio.run(self._discover_server_tools(server_name, server_config))
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()

        if errors:
            raise errors[0]
        return result[0] if result else []

    async def _discover_server_tools(
        self,
        server_name: str,
        server_config: dict[str, Any],
    ) -> list[AgentTool]:
        """Discover real tools from an MCP server and convert them to AgentTools."""
        tools_result = await self._list_server_tools(server_name, server_config)
        return self._agent_tools_from_remote_tools(server_name, tools_result)

    def _create_proxy_tools(self, server_name: str) -> list[AgentTool]:
        """Create a fallback proxy tool when eager discovery is not possible."""
        safe_server_name = self._safe_tool_name_part(server_name)

        async def mcp_proxy_tool(
            args: dict[str, Any],
            _context: Any | None = None,
            *,
            _server_name: str = server_name,
        ) -> dict[str, Any]:
            tool_name = args.get("tool_name")
            if not tool_name:
                return {"error": "Missing required field: tool_name"}
            arguments = args.get("arguments") or {}
            return await self._execute_mcp_tool(_server_name, tool_name, arguments)

        return [
            AgentTool(
                name=f"{self.tool_name_prefix}_{safe_server_name}_call",
                description=(
                    f"Fallback MCP proxy for server '{server_name}'. "
                    "Use when tool discovery was deferred."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "Remote MCP tool name",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the remote MCP tool",
                        },
                    },
                    "required": ["tool_name"],
                },
                handler=mcp_proxy_tool,
            )
        ]

    def _agent_tools_from_remote_tools(
        self,
        server_name: str,
        remote_tools: list[Any],
    ) -> list[AgentTool]:
        """Convert remote MCP tool definitions to AgentTool objects."""
        agent_tools: list[AgentTool] = []

        for remote_tool in remote_tools:
            remote_tool_name = remote_tool.name
            if not self.is_tool_enabled(server_name, remote_tool_name):
                continue
            safe_server_name = self._safe_tool_name_part(server_name)
            safe_remote_tool_name = self._safe_tool_name_part(remote_tool_name)
            local_tool_name = (
                f"{self.tool_name_prefix}_{safe_server_name}_{safe_remote_tool_name}"
            )
            description = remote_tool.description or (
                f"MCP tool '{remote_tool_name}' from server '{server_name}'"
            )
            parameters = remote_tool.inputSchema or {
                "type": "object",
                "properties": {},
            }

            async def mcp_tool_handler(
                args: dict[str, Any],
                _context: Any | None = None,
                *,
                _server_name: str = server_name,
                _remote_tool_name: str = remote_tool_name,
            ) -> dict[str, Any]:
                return await self._execute_mcp_tool(
                    _server_name,
                    _remote_tool_name,
                    args,
                )

            agent_tools.append(
                AgentTool(
                    name=local_tool_name,
                    description=description,
                    parameters=parameters,
                    handler=mcp_tool_handler,
                )
            )

        return agent_tools

    async def _execute_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a tool call on an MCP server, reusing a pooled session.

        The first call to a given server pays connection setup cost (spawn
        subprocess / open SSE / open HTTP stream). Every subsequent call to
        the same server reuses that same session — no reconnect, no
        re-``initialize()``. If the cached session turns out to be dead
        (broken pipe, closed transport, etc.), it's dropped so the *next*
        call transparently reconnects instead of staying wedged.
        """
        server_config = self.servers[server_name]
        transport = server_config.get("transport", "stdio").lower()

        if not self.is_server_enabled(server_name):
            return {
                "error": f"MCP server '{server_name}' is disabled",
                "error_type": "ServerDisabled",
                "server": server_name,
                "tool": tool_name,
            }

        if not self.is_tool_enabled(server_name, tool_name):
            return {
                "error": (
                    f"MCP tool '{tool_name}' on server '{server_name}' is disabled"
                ),
                "error_type": "ToolDisabled",
                "server": server_name,
                "tool": tool_name,
            }

        if transport not in ("stdio", "sse", "http", "streamable_http"):
            return {
                "error": f"Unsupported transport: {transport}",
                "supported": ["stdio", "sse", "http"],
                "server": server_name,
                "tool": tool_name,
            }

        try:
            session = await self._get_session(server_name)
            return await self._call_tool_on_cached_session(
                session, server_name, tool_name, arguments
            )
        except Exception as exc:
            # Drop the (possibly broken) cached session/tool list so the
            # next call gets a fresh connection instead of repeating the
            # same failure forever.
            self.sessions.pop(server_name, None)
            self._server_tool_lists.pop(server_name, None)
            return {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "server": server_name,
                "tool": tool_name,
                "transport": transport,
                "server_target": self._server_target(server_config),
                "arguments": arguments,
            }

    async def _get_session(self, server_name: str) -> "ClientSession":
        """Return the pooled session for ``server_name``, connecting once.

        Connections are entered into ``self._exit_stack`` so they stay open
        until ``cleanup()``/``aclose()`` tears them down, instead of closing
        at the end of an ``async with`` block like the previous per-call
        implementation did.
        """
        existing = self.sessions.get(server_name)
        if existing is not None:
            return existing

        lock = self._session_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            existing = self.sessions.get(server_name)
            if existing is not None:
                return existing

            server_config = self.servers[server_name]
            transport = server_config.get("transport", "stdio").lower()

            if transport == "stdio":
                command = server_config.get("command", "node")
                args = server_config.get("args", [])
                env = server_config.get("env", {})
                server_params = StdioServerParameters(
                    command=command, args=args, env=env if env else None
                )
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
            elif transport == "sse":
                url = server_config.get("url")
                if not url:
                    raise ValueError("SSE transport requires 'url' in config")
                headers = server_config.get("headers", {})
                read, write = await self._exit_stack.enter_async_context(
                    sse_client(url, headers=headers)
                )
            elif transport in ("http", "streamable_http"):
                url = server_config.get("url")
                if not url:
                    raise ValueError("HTTP transport requires 'url' in config")
                headers = server_config.get("headers", {})
                http_client = None
                if headers:
                    import httpx

                    http_client = await self._exit_stack.enter_async_context(
                        httpx.AsyncClient(headers=headers)
                    )
                http_transport = await self._exit_stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
                read, write, _get_session_id = http_transport
            else:
                raise ValueError(f"Unsupported transport: {transport}")

            session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self.sessions[server_name] = session
            return session

    async def _call_tool_on_cached_session(
        self,
        session: "ClientSession",
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool on an already-initialized, pooled session.

        The remote tool list is fetched once per server (on first use) and
        cached, instead of re-listing tools on every single call.
        """
        tools_result = self._server_tool_lists.get(server_name)
        if tools_result is None:
            tools_result = (await session.list_tools()).tools
            self._server_tool_lists[server_name] = tools_result

        tool_found = next((t for t in tools_result if t.name == tool_name), None)
        if not tool_found:
            return {
                "error": f"Tool '{tool_name}' not found",
                "error_type": "ToolNotFound",
                "available_tools": [t.name for t in tools_result],
                "server": server_name,
                "tool": tool_name,
                "arguments": arguments,
            }

        result = await session.call_tool(tool_name, arguments)
        return {
            "success": True,
            "result": self._serialize_mcp_value(
                result.content if hasattr(result, "content") else result
            ),
            "tool": tool_name,
            "server": server_name,
        }

    async def _list_server_tools(
        self,
        server_name: str,
        server_config: dict[str, Any],
    ) -> list[Any]:
        """List available tools from an MCP server."""
        transport = server_config.get("transport", "stdio").lower()

        if transport == "stdio":
            return await self._list_stdio_tools(server_name, server_config)
        elif transport == "sse":
            return await self._list_sse_tools(server_name, server_config)
        elif transport in ("http", "streamable_http"):
            return await self._list_http_tools(server_name, server_config)
        return []

    async def _list_stdio_tools(
        self,
        server_name: str,
        server_config: dict[str, Any],
    ) -> list[Any]:
        command = server_config.get("command", "node")
        args = server_config.get("args", [])
        env = server_config.get("env", {})

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env if env else None,
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return list(result.tools)

    async def _list_sse_tools(
        self,
        server_name: str,
        server_config: dict[str, Any],
    ) -> list[Any]:
        url = server_config.get("url")
        if not url:
            return []
        headers = server_config.get("headers", {})

        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return list(result.tools)

    async def _list_http_tools(
        self,
        server_name: str,
        server_config: dict[str, Any],
    ) -> list[Any]:
        url = server_config.get("url")
        if not url:
            return []
        headers = server_config.get("headers", {})

        import httpx

        http_client = httpx.AsyncClient(headers=headers) if headers else None
        try:
            async with streamable_http_client(
                url,
                http_client=http_client,
            ) as transport:
                read, write, _get_session_id = transport
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return list(result.tools)
        finally:
            if http_client is not None:
                await http_client.aclose()

    def _serialize_mcp_value(self, value: Any) -> Any:
        """Convert MCP SDK objects into JSON-serializable Python values."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, list):
            return [self._serialize_mcp_value(item) for item in value]

        if isinstance(value, tuple):
            return [self._serialize_mcp_value(item) for item in value]

        if isinstance(value, dict):
            return {
                str(key): self._serialize_mcp_value(item) for key, item in value.items()
            }

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return self._serialize_mcp_value(model_dump())

        if hasattr(value, "__dict__"):
            return self._serialize_mcp_value(vars(value))

        return str(value)

    def _server_target(self, server_config: dict[str, Any]) -> str:
        """Return a human-readable target for a server config."""
        transport = server_config.get("transport", "stdio").lower()
        if transport == "stdio":
            command = server_config.get("command", "")
            args = server_config.get("args", [])
            return " ".join([str(command), *[str(arg) for arg in args]]).strip()
        return str(server_config.get("url", ""))

    @staticmethod
    def _safe_tool_name_part(value: str) -> str:
        """Normalize a tool-name component for LLM provider compatibility."""
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
        return normalized or "tool"

    def is_server_enabled(self, server_name: str) -> bool:
        """Return whether ``server_name`` is active (default: enabled).

        An explicit ``"enabled": false`` in the server's config disables the
        whole server: none of its tools are discovered or exposed.
        """
        return bool(self.servers.get(server_name, {}).get("enabled", True))

    def is_tool_enabled(self, server_name: str, remote_tool_name: str) -> bool:
        """Return whether a single remote tool of a server is active.

        The per-tool map is ``server_config["tools"][remote_name]``. A missing
        map (or a missing entry) means *enabled* — only an explicit
        ``false`` turns a tool off.
        """
        server_config = self.servers.get(server_name, {})
        tools_map = server_config.get("tools") or {}
        return bool(tools_map.get(remote_tool_name, True))

    def get_tools(self) -> list[AgentTool]:
        """Return tools from all configured MCP servers."""
        return self.tools_cache

    def cleanup(self) -> None:
        """Cleanup MCP server connections.

        Pooled sessions hold real async resources (subprocesses, SSE/HTTP
        streams) that must be torn down with an awaited ``AsyncExitStack``.
        This sync hook only manages that when it's safe to spin up a
        throwaway event loop (no loop already running). When called from
        inside an async context (e.g. the CLI), prefer awaiting
        :meth:`aclose` directly before/instead of this method — otherwise
        connections are dropped from bookkeeping without being closed.
        """
        try:
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False

        if not running_loop:
            try:
                asyncio.run(self._exit_stack.aclose())
            except Exception:
                pass
            self._exit_stack = AsyncExitStack()

        self.sessions.clear()
        self._server_tool_lists.clear()
        self.tools_cache.clear()
        self._initialized = False

    async def aclose(self) -> None:
        """Async, awaitable teardown of every pooled MCP connection.

        Prefer this over :meth:`cleanup` when already inside an event loop.
        """
        await self._exit_stack.aclose()
        self._exit_stack = AsyncExitStack()
        self.sessions.clear()
        self._server_tool_lists.clear()
        self.tools_cache.clear()
        self._initialized = False


def create_plugin() -> MCPPlugin:
    """Factory function to create an MCP plugin instance."""
    return MCPPlugin()
