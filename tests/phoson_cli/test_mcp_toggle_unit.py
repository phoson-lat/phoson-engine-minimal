"""Tests for I-100 — /mcp toggle (server-level and tool-level MCP toggles)."""

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from phoson_cli._mcp_commands import toggle_mcp_config

SAMPLE = {
    "mcpServers": {
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        },
        "github": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
        },
    }
}


def _write(tmp_path: Path, data: dict | None = None) -> Path:
    path = tmp_path / "mcps.json"
    path.write_text(json.dumps(data if data is not None else SAMPLE, indent=2))
    return path


# ─── toggle_mcp_config: server level ───────────────────────────────────────


class TestToggleServer:
    def test_flip_enabled_off(self, tmp_path):
        path = _write(tmp_path)
        target, new_state = toggle_mcp_config(path, "filesystem")

        assert (target, new_state) == ("filesystem", False)
        data = json.loads(path.read_text())
        assert data["mcpServers"]["filesystem"]["enabled"] is False
        # other servers untouched
        assert "enabled" not in data["mcpServers"]["github"]

    def test_flip_off_to_on(self, tmp_path):
        data = json.loads(json.dumps(SAMPLE))
        data["mcpServers"]["filesystem"]["enabled"] = False
        path = _write(tmp_path, data)
        target, new_state = toggle_mcp_config(path, "filesystem")

        assert (target, new_state) == ("filesystem", True)
        assert (
            json.loads(path.read_text())["mcpServers"]["filesystem"]["enabled"] is True
        )

    def test_unknown_server(self, tmp_path):
        path = _write(tmp_path)
        with pytest.raises(ValueError, match="Unknown MCP server 'nope'"):
            toggle_mcp_config(path, "nope")

    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            toggle_mcp_config(tmp_path / "nope.json", "filesystem")

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "mcps.json"
        path.write_text("{ not json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            toggle_mcp_config(path, "filesystem")

    def test_creates_backup(self, tmp_path):
        path = _write(tmp_path)
        toggle_mcp_config(path, "filesystem")
        backup = tmp_path / "mcps.json.bak"
        assert backup.exists()
        # backup holds the pre-toggle content
        assert (
            "enabled" not in json.loads(backup.read_text())["mcpServers"]["filesystem"]
        )


# ─── toggle_mcp_config: tool level ─────────────────────────────────────────


class TestToggleTool:
    def test_flip_tool_off_by_remote_name(self, tmp_path):
        path = _write(tmp_path)
        target, new_state = toggle_mcp_config(path, "filesystem", "read_file")

        assert new_state is False
        data = json.loads(path.read_text())
        assert data["mcpServers"]["filesystem"]["tools"] == {"read_file": False}

    def test_flip_tool_back_on(self, tmp_path):
        data = json.loads(json.dumps(SAMPLE))
        data["mcpServers"]["filesystem"]["tools"] = {"read_file": False}
        path = _write(tmp_path, data)
        target, new_state = toggle_mcp_config(path, "filesystem", "read_file")

        assert new_state is True
        assert json.loads(path.read_text())["mcpServers"]["filesystem"]["tools"] == {
            "read_file": True
        }

    def test_local_prefixed_name_resolves_to_remote(self, tmp_path):
        path = _write(tmp_path)
        # `mcp_filesystem_read_file` is the local name the model sees.
        target, new_state = toggle_mcp_config(
            path, "filesystem", "mcp_filesystem_read_file"
        )

        assert new_state is False
        data = json.loads(path.read_text())
        assert data["mcpServers"]["filesystem"]["tools"] == {"read_file": False}

    def test_local_name_resolved_against_existing_tools_map(self, tmp_path):
        # A remote name with a dot: its local form is safe-formatted
        # (`read.file` → `mcp_filesystem_read_file`), so the inverse
        # resolution must consult the existing tools map.
        data = json.loads(json.dumps(SAMPLE))
        data["mcpServers"]["filesystem"]["tools"] = {"read.file": True}
        path = _write(tmp_path, data)
        toggle_mcp_config(path, "filesystem", "mcp_filesystem_read_file")

        stored = json.loads(path.read_text())["mcpServers"]["filesystem"]["tools"]
        assert stored == {"read.file": False}

    def test_tool_flip_does_not_touch_server_enabled(self, tmp_path):
        path = _write(tmp_path)
        toggle_mcp_config(path, "filesystem", "read_file")
        server_cfg = json.loads(path.read_text())["mcpServers"]["filesystem"]
        assert "enabled" not in server_cfg


# ─── /mcp toggle subcommand wiring ─────────────────────────────────────────


def _make_subcommands(config, *, mcp_enabled: bool = True, plugin_servers=None):
    """Build _MCPSubcommands with a minimal repl/host double."""
    from phoson_cli._mcp_commands import _MCPSubcommands

    class _Host:
        def __init__(self) -> None:
            self.infos: list[str] = []
            self.warns: list[str] = []
            self.errors: list[str] = []

        def print_info(self, m: str) -> None:
            self.infos.append(m)

        def print_warn(self, m: str) -> None:
            self.warns.append(m)

        def print_error(self, m: str) -> None:
            self.errors.append(m)

    host = _Host()

    repl = SimpleNamespace(
        config=config,
        current_model="test-model",
        set_model=AsyncMock(),
        engine=_FakeEngine(plugin_servers),
    )
    parent = SimpleNamespace(repl=repl, host=host)
    return _MCPSubcommands(parent), host


class _FakeEngine:
    def __init__(self, plugin_servers) -> None:
        self._loaded_plugins = [
            _FakeMcpPlugin(plugin_servers or {}),
        ]


class _FakeMcpPlugin:
    name = "phoson-plugin-mcp"
    tool_name_prefix = "mcp"

    def __init__(self, servers: dict) -> None:
        self.servers = servers


class TestToggleSubcommand:
    async def test_toggle_server_persists_and_reapplies(self, tmp_path):
        config_path = _write(tmp_path)

        class _Cfg:
            enable_mcp = True
            mcp_config_file = config_path

        sub, host = _make_subcommands(_Cfg())
        from phoson_cli.commands import Command

        result = await sub.dispatch(Command(name="/mcp", args="toggle filesystem"))

        assert result is True
        assert host.errors == []
        assert any("❌ filesystem → disabled" in i for i in host.infos)
        assert (
            json.loads(config_path.read_text())["mcpServers"]["filesystem"]["enabled"]
            is False
        )
        # in-flight reapply via set_model (engine rebuild)
        sub.repl.set_model.assert_awaited_once_with("test-model")

    async def test_toggle_tool_persists(self, tmp_path):
        config_path = _write(tmp_path)

        class _Cfg:
            enable_mcp = True
            mcp_config_file = config_path

        sub, host = _make_subcommands(_Cfg())
        from phoson_cli.commands import Command

        await sub.dispatch(Command(name="/mcp", args="toggle filesystem read_file"))

        assert json.loads(config_path.read_text())["mcpServers"]["filesystem"][
            "tools"
        ] == {"read_file": False}

    async def test_toggle_unknown_server_shows_error(self, tmp_path):
        config_path = _write(tmp_path)

        class _Cfg:
            enable_mcp = True
            mcp_config_file = config_path

        sub, host = _make_subcommands(_Cfg())
        from phoson_cli.commands import Command

        await sub.dispatch(Command(name="/mcp", args="toggle nope"))

        assert any("Unknown MCP server" in e for e in host.errors)
        sub.repl.set_model.assert_not_awaited()

    async def test_toggle_without_args_shows_usage(self, tmp_path):
        config_path = _write(tmp_path)

        class _Cfg:
            enable_mcp = True
            mcp_config_file = config_path

        sub, host = _make_subcommands(_Cfg())
        from phoson_cli.commands import Command

        await sub.dispatch(Command(name="/mcp", args="toggle"))

        assert any("Usage: /mcp toggle" in e for e in host.errors)

    async def test_toggle_warns_when_mcp_globally_off(self, tmp_path):
        config_path = _write(tmp_path)

        class _Cfg:
            enable_mcp = False
            mcp_config_file = config_path

        sub, host = _make_subcommands(_Cfg())
        from phoson_cli.commands import Command

        await sub.dispatch(Command(name="/mcp", args="toggle filesystem"))

        # persisted…
        assert (
            json.loads(config_path.read_text())["mcpServers"]["filesystem"]["enabled"]
            is False
        )
        # …but not applied, and warned
        sub.repl.set_model.assert_not_awaited()
        assert any("globally disabled" in w for w in host.warns)
