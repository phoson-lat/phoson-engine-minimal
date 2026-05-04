"""
Phoson MCP Plugin

Integrates Model Context Protocol (MCP) servers with Phoson Agent.
Loads MCP server configurations from phoson-mcp.json and exposes their tools.
"""

from .plugin import MCPPlugin

__version__ = "0.1.0"

# Export plugin instance
plugin = MCPPlugin()

__all__ = ["MCPPlugin", "plugin"]
