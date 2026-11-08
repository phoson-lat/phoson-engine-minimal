"""
Unit tests for MCP plugin.
"""

import sys
import json
import asyncio
from types import SimpleNamespace
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
    from phoson_plugin_mcp._plugin import MCP_AVAILABLE

    # MCPPlugin itself always imports fine (phoson_plugin_mcp degrades
    # gracefully without the `mcp` SDK); what these tests actually need is
    # the real `mcp` package, tracked by _plugin.py's own MCP_AVAILABLE.
except ImportError as e:
    MCP_AVAILABLE = False
    MCPPlugin = None
    print(f"Warning: Could not import MCPPlugin: {e}")


@pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp package not installed")
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

        # servers dict (static config) is preserved; runtime state is cleared
        assert len(plugin.servers) == 1
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


@pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp package not installed")
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
                    "name": "path:./phoson_plugin_mcp/_plugin.py",
                    "config": {"config_file": str(config_file)},
                }
            ],
        )

        # Verify plugin loaded
        assert len(engine._loaded_plugins) == 1
        assert engine._loaded_plugins[0].name == "phoson-plugin-mcp"


@pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp package not installed")
class TestMCPToggles:
    """I-100: server-level and tool-level enable/disable flags."""

    def _plugin_with_servers(self, servers: dict) -> "MCPPlugin":
        plugin = MCPPlugin()
        plugin.servers = servers
        return plugin

    def test_is_server_enabled_default_true(self):
        plugin = self._plugin_with_servers({"a": {}})
        assert plugin.is_server_enabled("a") is True

    def test_is_server_enabled_explicit_false(self):
        plugin = self._plugin_with_servers({"a": {"enabled": False}})
        assert plugin.is_server_enabled("a") is False

    def test_is_server_enabled_unknown_server(self):
        plugin = self._plugin_with_servers({})
        assert plugin.is_server_enabled("nope") is True

    def test_is_tool_enabled_default_true(self):
        plugin = self._plugin_with_servers({"a": {}})
        assert plugin.is_tool_enabled("a", "read_file") is True

    def test_is_tool_enabled_explicit_false(self):
        plugin = self._plugin_with_servers({"a": {"tools": {"read_file": False}}})
        assert plugin.is_tool_enabled("a", "read_file") is False
        assert plugin.is_tool_enabled("a", "write_file") is True

    def test_is_tool_enabled_empty_map_all_active(self):
        plugin = self._plugin_with_servers({"a": {"tools": {}}})
        assert plugin.is_tool_enabled("a", "read_file") is True

    def test_disabled_server_exposes_no_proxy_tools(self):
        """A disabled server must not register even the deferred proxy tool."""
        plugin = MCPPlugin()
        plugin.servers = {
            "on": {"command": "node"},
            "off": {"command": "node", "enabled": False},
        }
        # Simulate deferred discovery: tools_cache empty -> proxy tools.
        for server_name in plugin.servers.keys():
            if plugin.is_server_enabled(server_name):
                plugin.tools_cache.extend(plugin._create_proxy_tools(server_name))

        names = [t.name for t in plugin.get_tools()]
        assert "mcp_on_call" in names
        assert not any(n.startswith("mcp_off_") for n in names)

    def test_tool_filter_drops_disabled_tools(self):
        """Remote tools with an explicit False in the tools map are skipped."""
        plugin = self._plugin_with_servers({"fs": {"tools": {"read_file": False}}})
        remote = [
            SimpleNamespace(name="read_file", description="read", inputSchema={}),
            SimpleNamespace(name="write_file", description="write", inputSchema={}),
        ]
        tools = plugin._agent_tools_from_remote_tools("fs", remote)
        names = [t.name for t in tools]
        assert names == ["mcp_fs_write_file"]

    def test_disabled_server_is_skipped_during_discovery(self):
        """`initialize` must not even connect to a disabled server."""
        plugin = MCPPlugin()
        plugin.config_file = Path("/nonexistent/phoson-mcp.json")
        plugin.servers = {
            "on": {"command": "node"},
            "off": {"command": "node", "enabled": False},
        }

        discovered: list[str] = []

        async def fake_discover(server_name, server_config):
            discovered.append(server_name)
            remote = SimpleNamespace(name="t", description="d", inputSchema={})
            return plugin._agent_tools_from_remote_tools(server_name, [remote])

        plugin._discover_server_tools = fake_discover
        plugin._load_tools_from_servers()

        assert discovered == ["on"]
        assert [t.name for t in plugin.get_tools()] == ["mcp_on_t"]

    def _parallel_plugin(self, servers, delays):
        plugin = MCPPlugin()
        plugin.config_file = Path("/nonexistent/phoson-mcp.json")
        plugin.servers = servers
        started: list[str] = []

        async def fake_discover(server_name, server_config):
            started.append(server_name)
            await asyncio.sleep(delays[server_name])
            if delays[server_name] < 0:
                raise RuntimeError("boom")
            remote = SimpleNamespace(name="t", description="d", inputSchema={})
            return plugin._agent_tools_from_remote_tools(server_name, [remote])

        plugin._discover_server_tools = fake_discover
        return plugin, started

    def test_servers_are_discovered_concurrently_in_config_order(self):
        import time

        plugin, _ = self._parallel_plugin(
            {
                "slow": {"command": "x"},
                "fast": {"command": "x"},
                "mid": {"command": "x"},
            },
            {"slow": 0.4, "fast": 0.0, "mid": 0.2},
        )
        start = time.monotonic()
        plugin._load_tools_from_servers()
        elapsed = time.monotonic() - start

        assert elapsed < 0.55  # sequential would take >= 0.6 s
        assert [t.name for t in plugin.get_tools()] == [
            "mcp_slow_t",
            "mcp_fast_t",
            "mcp_mid_t",
        ]

    def test_hung_or_failing_server_does_not_block_others(self, capsys):
        plugin, _ = self._parallel_plugin(
            {
                "hung": {"command": "x", "startup_timeout": 0.1},
                "bad": {"command": "x"},
                "ok": {"command": "x"},
            },
            {"hung": 30.0, "bad": -1, "ok": 0.0},
        )
        plugin._load_tools_from_servers()

        assert [t.name for t in plugin.get_tools()] == ["mcp_ok_t"]
        out = capsys.readouterr().out
        assert "'hung': timed out" in out
        assert "'bad': boom" in out

    def test_concurrent_discovery_inside_a_running_loop(self):
        plugin, _ = self._parallel_plugin(
            {"a": {"command": "x"}, "b": {"command": "x"}}, {"a": 0.0, "b": 0.0}
        )

        async def inside_loop():
            plugin._load_tools_from_servers()

        asyncio.run(inside_loop())
        assert [t.name for t in plugin.get_tools()] == ["mcp_a_t", "mcp_b_t"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, 30.0), (5, 5.0), ("2.5", 2.5), (0, 30.0), ("x", 30.0)],
    )
    def test_startup_timeout_parsing(self, value, expected):
        from phoson_plugin_mcp._plugin import _startup_timeout

        config = {} if value is None else {"startup_timeout": value}
        assert _startup_timeout(config) == expected

    def test_execution_guard_disabled_server(self):
        plugin = self._plugin_with_servers({"off": {"enabled": False}})

        async def run():
            return await plugin._execute_mcp_tool("off", "read_file", {})

        result = asyncio.run(run())
        assert result["error_type"] == "ServerDisabled"

    def test_execution_guard_disabled_tool(self):
        plugin = self._plugin_with_servers(
            {"fs": {"command": "node", "tools": {"read_file": False}}}
        )

        async def run():
            return await plugin._execute_mcp_tool("fs", "read_file", {})

        result = asyncio.run(run())
        assert result["error_type"] == "ToolDisabled"


class TestMCPPluginWithoutMCP:
    """Test plugin behavior when MCP is not installed."""

    def test_initialize_without_mcp_raises(self):
        """Test that initialization fails gracefully without MCP."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP is not installed, can't test this scenario")

        # This test would need to mock the MCP_AVAILABLE flag
        # For now, we just verify the plugin can be imported
        assert MCPPlugin is not None


class TestStdioChildEnvPassthrough:
    """Regression: GUI-launching stdio MCP servers need session env vars.

    The MCP SDK's default allowlist only forwards
    HOME/LOGNAME/PATH/SHELL/TERM/USER.  Without DISPLAY/WAYLAND_DISPLAY/
    XAUTHORITY/XDG_RUNTIME_DIR, chrome-devtools-mcp fails with
    "Missing X server to start the headful browser".
    """

    def test_forwards_session_display_vars(self, monkeypatch):
        from phoson_plugin_mcp._plugin import _stdio_child_env

        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("XAUTHORITY", "/run/user/1000/.mutter-Xwaylandauth.X")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

        env = _stdio_child_env({})

        assert env["DISPLAY"] == ":0"
        assert env["WAYLAND_DISPLAY"] == "wayland-0"
        assert env["XAUTHORITY"].endswith(".X")
        assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"

    def test_configured_env_wins_and_secrets_are_not_leaked(self, monkeypatch):
        from phoson_plugin_mcp._plugin import _stdio_child_env

        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")

        env = _stdio_child_env({"DISPLAY": ":9", "FOO": "bar"})

        assert env["DISPLAY"] == ":9"
        assert env["FOO"] == "bar"
        assert "OPENROUTER_API_KEY" not in env


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
