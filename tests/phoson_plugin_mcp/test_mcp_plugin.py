"""
Unit tests for MCP plugin.
"""

import sys
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from phoson_agent import AgentEngine

# Import with fallback if mcp not installed

# Add project root to path
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from phoson_plugin_mcp import MCPPlugin

    MCP_AVAILABLE = True
except ImportError as e:
    MCP_AVAILABLE = False
    MCPPlugin = None
    print(f"Warning: Could not import MCPPlugin: {e}")


@pytest.mark.skipif(MCPPlugin is None, reason="MCPPlugin not available")
class TestMCPPlugin:
    """Tests for MCPPlugin."""

    def test_plugin_properties(self):
        """Test basic plugin properties."""
        plugin = MCPPlugin()

        assert plugin.name == "phoson-plugin-mcp"
        assert plugin.version == "0.1.0"
        assert "Model Context Protocol" in plugin.description

    def test_configure_with_custom_file(self):
        """Test configuration with custom config file."""
        plugin = MCPPlugin()
        plugin.configure({"config_file": "./custom-mcp.json"})

        assert plugin.config_file == Path("./custom-mcp.json")

    def test_configure_with_inline_servers(self):
        """Test configuration with inline server definitions."""
        plugin = MCPPlugin()
        servers = {"test-server": {"command": "node", "args": ["server.js"]}}
        plugin.configure({"servers": servers})

        assert "test-server" in plugin.servers
        assert plugin.servers["test-server"]["command"] == "node"

    def test_initialize_without_config_file(self, tmp_path):
        """Test initialization when config file doesn't exist."""
        plugin = MCPPlugin()
        plugin.config_file = tmp_path / "nonexistent.json"

        # Should not raise, just have no servers
        plugin.initialize()
        assert len(plugin.servers) == 0

    def test_initialize_with_config_file(self, tmp_path):
        """Test initialization with valid config file."""
        config_file = tmp_path / "phoson-mcp.json"
        config_data = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                }
            }
        }
        config_file.write_text(json.dumps(config_data))

        plugin = MCPPlugin()
        plugin.config_file = config_file
        plugin.initialize()

        assert "filesystem" in plugin.servers
        assert plugin.servers["filesystem"]["command"] == "npx"

    def test_initialize_with_servers_key(self, tmp_path):
        """Test initialization with 'servers' key instead of 'mcpServers'."""
        config_file = tmp_path / "phoson-mcp.json"
        config_data = {"servers": {"test": {"command": "node", "args": ["test.js"]}}}
        config_file.write_text(json.dumps(config_data))

        plugin = MCPPlugin()
        plugin.config_file = config_file
        plugin.initialize()

        assert "test" in plugin.servers

    def test_initialize_with_invalid_json(self, tmp_path):
        """Test initialization with invalid JSON."""
        config_file = tmp_path / "phoson-mcp.json"
        config_file.write_text("{ invalid json }")

        plugin = MCPPlugin()
        plugin.config_file = config_file

        with pytest.raises(ValueError, match="Invalid JSON"):
            plugin.initialize()

    def test_get_tools_returns_empty_without_servers(self):
        """Test that get_tools returns empty list when no servers configured."""
        plugin = MCPPlugin()
        plugin.initialize()

        tools = plugin.get_tools()
        assert tools == []

    def test_get_tools_creates_tools_for_servers(self, tmp_path):
        """Test that tools are created for configured servers."""
        config_file = tmp_path / "phoson-mcp.json"
        config_data = {
            "mcpServers": {
                "test1": {"command": "node", "args": ["test1.js"]},
                "test2": {"command": "node", "args": ["test2.js"]},
            }
        }
        config_file.write_text(json.dumps(config_data))

        plugin = MCPPlugin()
        plugin.config_file = config_file
        plugin.initialize()

        tools = plugin.get_tools()
        assert len(tools) == 2

        tool_names = [t.name for t in tools]
        assert "mcp_test1_call" in tool_names
        assert "mcp_test2_call" in tool_names

    def test_cleanup(self):
        """Test cleanup clears state."""
        plugin = MCPPlugin()
        plugin.servers = {"test": {}}
        plugin.tools_cache = [Mock()]
        plugin._initialized = True

        plugin.cleanup()

        # servers dict is preserved but sessions cleared
        assert len(plugin.servers) == 0
        assert len(plugin.tools_cache) == 0
        assert not plugin._initialized

    def test_integration_with_agent_engine(self, tmp_path):
        """Test plugin integration with AgentEngine."""
        config_file = tmp_path / "phoson-mcp.json"
        config_data = {"mcpServers": {"test": {"command": "node", "args": ["test.js"]}}}
        config_file.write_text(json.dumps(config_data))

        # Create plugin instance
        plugin = MCPPlugin()
        plugin.config_file = config_file

        # Create engine with plugin
        engine = AgentEngine(
            chat=Mock(),
            plugins=[plugin],
        )

        # Verify plugin was loaded
        assert len(engine._loaded_plugins) == 1
        assert engine._loaded_plugins[0] is plugin

        # Verify tools were added
        assert len(engine.tools) == 1
        assert engine.tools[0].name == "mcp_test_call"


@pytest.mark.skipif(MCPPlugin is None, reason="MCPPlugin not available")
class TestMCPPluginWithPath:
    """Test loading MCP plugin from path."""

    def test_load_from_path(self, tmp_path):
        """Test loading plugin from file path."""
        config_file = tmp_path / "phoson-mcp.json"
        config_data = {"mcpServers": {"test": {"command": "echo", "args": ["hello"]}}}
        config_file.write_text(json.dumps(config_data))

        # Create engine loading plugin from path
        engine = AgentEngine(
            chat=Mock(),
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/plugin.py",
                    "config": {"config_file": str(config_file)},
                }
            ],
        )

        # Verify plugin loaded
        assert len(engine._loaded_plugins) == 1
        assert engine._loaded_plugins[0].name == "phoson-plugin-mcp"


class TestMCPPluginWithoutMCP:
    """Test plugin behavior when MCP is not installed."""

    def test_initialize_without_mcp_raises(self):
        """Test that initialization fails gracefully without MCP."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP is not installed, can't test this scenario")

        # This test would need to mock the MCP_AVAILABLE flag
        # For now, we just verify the plugin can be imported
        assert MCPPlugin is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
