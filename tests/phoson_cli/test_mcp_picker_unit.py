"""Tests for the interactive ``/mcp toggle`` menu (bare form)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phoson_cli.commands import Command
from phoson_cli.mcp_picker import (
    McpToolView,
    McpServerView,
    McpPickerResult,
    build_mcp_picker,
)
from phoson_cli._mcp_commands import _MCPSubcommands

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


def _write(tmp_path, data=None):
    path = tmp_path / "mcps.json"
    path.write_text(json.dumps(data if data is not None else SAMPLE, indent=2))
    return path


# ─── Picker rendering ────────────────────────────────────────────────────────


def test_picker_renders_servers_and_nested_tools():
    servers = [
        McpServerView(
            "filesystem",
            "stdio",
            "npx -y server-filesystem /tmp",
            True,
            [McpToolView("read_file", True), McpToolView("write_file", False)],
        ),
        McpServerView("github", "stdio", "npx -y server-github", False, []),
    ]
    picker = build_mcp_picker(servers, on_toggle=lambda s, t: True)
    text = "".join(line for _style, line in picker._render())

    assert "MCP Servers" in text
    assert "filesystem" in text
    assert "● filesystem" in text
    assert "✓ read_file" in text
    assert "✗ write_file" in text
    # disabled server gets the dim/disabled marker
    assert "○ github" in text
    assert "(disabled)" in text
    assert "Enter/space toggle" in text


def _press(picker, key):
    for binding in picker._kb.get_bindings_for_keys((key,)):
        binding.handler(None)


def test_picker_scrolls_to_keep_selection_visible(monkeypatch):
    """A long server list must scroll so the cursor never leaves the screen."""

    class _Size:
        rows = 12

    class _Out:
        def get_size(self):
            return _Size()

    class _App:
        output = _Out()

    monkeypatch.setattr("prompt_toolkit.application.get_app", lambda: _App())

    servers = [
        McpServerView(
            "big",
            "stdio",
            "cmd",
            True,
            [McpToolView(f"tool_{i:02d}", True) for i in range(50)],
        )
    ]
    picker = build_mcp_picker(servers, on_toggle=lambda s, t: True)

    # Initially the window shows only the top of the list.
    text = "".join(line for _style, line in picker._render())
    assert "tool_00" in text
    assert "tool_45" not in text

    # Walking down to the bottom keeps the selected row in the viewport.
    for _ in range(46):
        _press(picker, "down")
    text = "".join(line for _style, line in picker._render())
    assert "▸ ✓ tool_45" in text
    # The range indicator reflects the scrolled window.
    assert "/51" in text


# ─── _MCPSubcommands wiring ──────────────────────────────────────────────────


class _FakeMcpPlugin:
    name = "phoson-plugin-mcp"
    tool_name_prefix = "mcp"

    def __init__(self, servers=None) -> None:
        self.servers = servers or {}
        self.tools_cache = []
        self._server_tool_lists = {}

    def is_tool_enabled(self, server, tool):
        tools_map = (self.servers.get(server) or {}).get("tools") or {}
        return bool(tools_map.get(tool, True))

    def is_server_enabled(self, server):
        return bool((self.servers.get(server) or {}).get("enabled", True))


class _FakeEngine:
    def __init__(self, plugin) -> None:
        self._loaded_plugins = [plugin]


class _PickHost:
    def __init__(self, picker=None) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.errors: list[str] = []
        self._picker = picker
        self.opened = 0

    def print_info(self, m: str) -> None:
        self.infos.append(m)

    def print_warn(self, m: str) -> None:
        self.warns.append(m)

    def print_error(self, m: str) -> None:
        self.errors.append(m)

    async def pick_mcp(self, servers, *, on_toggle):
        self.opened += 1
        if self._picker is None:
            raise AssertionError("picker unexpectedly opened")
        return await self._picker(servers, on_toggle)


def _make(config, *, plugin=None, picker=None):
    host = _PickHost(picker)
    repl = SimpleNamespace(
        config=config,
        current_model="test-model",
        set_model=AsyncMock(),
        engine=_FakeEngine(plugin or _FakeMcpPlugin()),
        theme=None,
    )
    parent = SimpleNamespace(repl=repl, host=host)
    return _MCPSubcommands(parent), host


async def test_bare_toggle_opens_menu_and_persists_changes(tmp_path):
    config_path = _write(tmp_path)

    class _Cfg:
        enable_mcp = True
        mcp_config_file = config_path

    seen: list[list[str]] = []

    async def _picker(servers, on_toggle):
        seen.append([s.name for s in servers])
        on_toggle("filesystem", None)
        on_toggle("filesystem", "read_file")
        return McpPickerResult(changes=2)

    sub, host = _make(_Cfg(), picker=_picker)
    result = await sub.dispatch(Command(name="/mcp", args="toggle"))

    assert result is True
    assert host.opened == 1
    assert seen == [["filesystem", "github"]]
    data = json.loads(config_path.read_text())
    assert data["mcpServers"]["filesystem"]["enabled"] is False
    assert data["mcpServers"]["filesystem"]["tools"] == {"read_file": False}
    # changes → engine rebuilt once
    sub.repl.set_model.assert_awaited_once_with("test-model")


async def test_bare_toggle_without_picker_capability_shows_usage(tmp_path):
    config_path = _write(tmp_path)

    class _Cfg:
        enable_mcp = True
        mcp_config_file = config_path

    host = _PickHost(picker=None)
    # A host that does not implement pick_mcp at all.
    plain_host = SimpleNamespace(
        print_info=host.print_info,
        print_warn=host.print_warn,
        print_error=host.print_error,
    )
    repl = SimpleNamespace(
        config=_Cfg(),
        current_model="test-model",
        set_model=AsyncMock(),
        engine=_FakeEngine(_FakeMcpPlugin()),
        theme=None,
    )
    sub = _MCPSubcommands(SimpleNamespace(repl=repl, host=plain_host))

    await sub.dispatch(Command(name="/mcp", args="toggle"))

    assert any("Usage: /mcp toggle" in e for e in host.errors)


async def test_bare_toggle_no_servers_configured(tmp_path):
    path = tmp_path / "mcps.json"
    path.write_text(json.dumps({"mcpServers": {}}))

    class _Cfg:
        enable_mcp = True
        mcp_config_file = path

    async def _picker(servers, on_toggle):  # pragma: no cover - must not run
        raise AssertionError("should not open with no servers")

    sub, host = _make(_Cfg(), picker=_picker)
    await sub.dispatch(Command(name="/mcp", args="toggle"))

    assert host.opened == 0
    assert any("No MCP servers configured" in e for e in host.errors)


def test_server_views_include_config_tool_map(tmp_path):
    data = json.loads(json.dumps(SAMPLE))
    data["mcpServers"]["filesystem"]["tools"] = {"read_file": False}
    config_path = _write(tmp_path, data)

    class _Cfg:
        enable_mcp = True
        mcp_config_file = config_path

    plugin = _FakeMcpPlugin({"filesystem": {"tools": {"read_file": False}}})
    sub, _host = _make(_Cfg(), plugin=plugin)
    views = {v.name: v for v in sub._server_views()}

    fs = views["filesystem"]
    tools = {t.name: t.enabled for t in fs.tools}
    assert tools == {"read_file": False}
    assert fs.enabled is True

    # Runtime-discovered tools with no explicit flag default to enabled.
    class _Tool:
        name = "list_dir"

    plugin._server_tool_lists = {"filesystem": [_Tool()]}
    views = {v.name: v for v in sub._server_views()}
    tools = {t.name: t.enabled for t in views["filesystem"].tools}
    assert tools == {"list_dir": True, "read_file": False}
