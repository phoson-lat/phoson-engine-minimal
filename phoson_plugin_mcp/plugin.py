"""
MCP Plugin implementation.
"""

import json
import asyncio
from pathlib import Path
from typing import Any
from collections.abc import Callable

from phoson_agent import Plugin, AgentTool, tool, AgentContext

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class MCPPlugin(Plugin):
    """
    Plugin for integrating Model Context Protocol (MCP) servers.
    
    Loads MCP server configurations from phoson-mcp.json and exposes
    their tools to the agent.
    
    Configuration file format (phoson-mcp.json):
    {
        "mcpServers": {
            "server-name": {
                "command": "node",
                "args": ["path/to/server.js"],
                "env": {
                    "API_KEY": "value"
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
        
        # Initialize tools from servers
        self._load_tools_from_servers()
        self._initialized = True
    
    def _load_tools_from_servers(self) -> None:
        """Load tools from all configured MCP servers."""
        # Note: MCP is async, but we need to work in sync context
        # We'll create tools that handle the async calls internally
        
        for server_name, server_config in self.servers.items():
            try:
                # Create tools for this server
                tools = self._create_server_tools(server_name, server_config)
                self.tools_cache.extend(tools)
            except Exception as e:
                print(f"Warning: Failed to load tools from MCP server '{server_name}': {e}")
    
    def _create_server_tools(
        self, 
        server_name: str, 
        server_config: dict[str, Any]
    ) -> list[AgentTool]:
        """Create tools for a specific MCP server."""
        # For now, we'll create a generic tool that connects to the server on demand
        # In a full implementation, we'd connect once and list available tools
        
        @tool
        def mcp_call_tool(tool_name: str, arguments: dict[str, Any] = None) -> dict[str, Any]:
            f"""
            Call a tool from the MCP server '{server_name}'.
            
            Args:
                tool_name: Name of the tool to call
                arguments: Arguments to pass to the tool
            """
            if arguments is None:
                arguments = {}
            
            # Execute the MCP call
            result = asyncio.run(
                self._execute_mcp_tool(server_name, tool_name, arguments)
            )
            return result
        
        # Rename the tool to be server-specific
        mcp_call_tool.name = f"mcp_{server_name}_call"
        mcp_call_tool.description = f"Call tools from MCP server '{server_name}'"
        
        return [mcp_call_tool]
    
    async def _execute_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a tool call on an MCP server."""
        server_config = self.servers[server_name]
        
        # Extract server parameters
        command = server_config.get("command", "node")
        args = server_config.get("args", [])
        env = server_config.get("env", {})
        
        # Create server parameters
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env if env else None
        )
        
        # Connect to server and execute tool
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
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
                        "available_tools": available
                    }
                
                # Call the tool
                result = await session.call_tool(tool_name, arguments)
                
                # Return the result
                return {
                    "success": True,
                    "result": result.content if hasattr(result, 'content') else result,
                    "tool": tool_name,
                    "server": server_name
                }
    
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
