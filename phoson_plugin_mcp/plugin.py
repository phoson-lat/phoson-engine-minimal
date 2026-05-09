"""
MCP Plugin implementation.
"""

import re
import json
from typing import Any
from pathlib import Path

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
                }
            }
        }
    }
    
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
        """Execute a tool call on an MCP server."""
        server_config = self.servers[server_name]

        # Determine transport type (default: stdio)
        transport = server_config.get("transport", "stdio").lower()

        # Connect based on transport type
        try:
            if transport == "stdio":
                return await self._execute_stdio(
                    server_name, server_config, tool_name, arguments
                )
            elif transport == "sse":
                return await self._execute_sse(
                    server_name, server_config, tool_name, arguments
                )
            elif transport in ("http", "streamable_http"):
                return await self._execute_http(
                    server_name, server_config, tool_name, arguments
                )
            else:
                return {
                    "error": f"Unsupported transport: {transport}",
                    "supported": ["stdio", "sse", "http"],
                    "server": server_name,
                    "tool": tool_name,
                }
        except Exception as exc:
            return {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "server": server_name,
                "tool": tool_name,
                "transport": transport,
                "server_target": self._server_target(server_config),
                "arguments": arguments,
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
    
    async def _execute_stdio(
        self,
        server_name: str,
        server_config: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute tool via STDIO transport."""
        command = server_config.get("command", "node")
        args = server_config.get("args", [])
        env = server_config.get("env", {})
        
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env if env else None
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                return await self._call_tool_on_session(
                    session, server_name, tool_name, arguments
                )
    
    async def _execute_sse(
        self,
        server_name: str,
        server_config: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute tool via SSE transport."""
        url = server_config.get("url")
        if not url:
            return {"error": "SSE transport requires 'url' in config"}
        
        headers = server_config.get("headers", {})
        
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                return await self._call_tool_on_session(
                    session, server_name, tool_name, arguments
                )
    
    async def _execute_http(
        self,
        server_name: str,
        server_config: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute tool via HTTP transport."""
        url = server_config.get("url")
        if not url:
            return {"error": "HTTP transport requires 'url' in config"}
        
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
                    return await self._call_tool_on_session(
                        session, server_name, tool_name, arguments
                    )
        finally:
            if http_client is not None:
                await http_client.aclose()
    
    async def _call_tool_on_session(
        self,
        session: ClientSession,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool on an established session."""
        # Initialize the session
        await session.initialize()

        # List available tools
        tools_result = await session.list_tools()

        # Find the requested tool
        tool_found = None
        for tool_info in tools_result.tools:
            if tool_info.name == tool_name:
                tool_found = tool_info
                break

        if not tool_found:
            available = [t.name for t in tools_result.tools]
            return {
                "error": f"Tool '{tool_name}' not found",
                "error_type": "ToolNotFound",
                "available_tools": available,
                "server": server_name,
                "tool": tool_name,
                "arguments": arguments,
            }

        # Call the tool
        result = await session.call_tool(tool_name, arguments)

        # Return the result
        return {
            "success": True,
            "result": self._serialize_mcp_value(
                result.content if hasattr(result, 'content') else result
            ),
            "tool": tool_name,
            "server": server_name,
        }

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
                str(key): self._serialize_mcp_value(item)
                for key, item in value.items()
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
    
    def get_tools(self) -> list[AgentTool]:
        """Return tools from all configured MCP servers."""
        return self.tools_cache
    
    def cleanup(self) -> None:
        """Cleanup MCP server connections."""
        # Close any open sessions
        for session in self.sessions.values():
            try:
                # Sessions are async context managers, cleanup happens automatically
                pass
            except Exception:
                pass
        
        self.sessions.clear()
        self.tools_cache.clear()
        self._initialized = False


def create_plugin() -> MCPPlugin:
    """Factory function to create an MCP plugin instance."""
    return MCPPlugin()
