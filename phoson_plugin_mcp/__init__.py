"""
Phoson MCP Plugin

Integrates Model Context Protocol (MCP) servers with Phoson Agent.
Loads MCP server configurations from phoson-mcp.json and exposes their tools.
"""

from ._plugin import MCPPlugin

__version__ = "0.1.0"

# Export plugin instance. NOTE: the module file is named `_plugin.py` (not
# `plugin.py`) so this `plugin = ...` attribute does not shadow the
# submodule attribute — otherwise `import phoson_plugin_mcp.plugin as m`
# would bind the instance instead of the module.
plugin = MCPPlugin()

__all__ = ["MCPPlugin", "plugin"]
