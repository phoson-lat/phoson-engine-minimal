"""Browser use via the official Chrome DevTools MCP server (#224): the MCP
entry per mode, URL patterns, config file handling, detection, the /browser
command, and per-tool annotation overrides in the MCP plugin."""

import os
import sys
import json
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from phoson_cli import browser
from phoson_cli.commands import Command, CommandHandler


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOSON_HOME", str(tmp_path / ".phoson"))
    return tmp_path


def test_profile_entry_is_private_pinned_and_lean() -> None:
    entry = browser.build_server_entry(browser.BrowserSettings())
    args = entry["args"]
    assert args[:2] == ["-y", f"chrome-devtools-mcp@{browser.SERVER_VERSION}"]
    assert "@latest" not in " ".join(args)
    assert "--no-usage-statistics" in args and "--performanceCrux=false" in args
    assert "--redactNetworkHeaders=true" in args
    assert f"--userDataDir={browser.profile_dir()}" in args
    assert "--autoConnect" not in args
    assert entry["env"]["CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS"] == "1"
    assert entry["startup_timeout"] >= 60
    assert all(entry["tools"][name] is False for name in browser.DISABLED_TOOLS)
    look = entry["annotations"]
    assert look["take_snapshot"]["readOnlyHint"] is True
    assert look["navigate_page"]["readOnlyHint"] is True  # isolated profile
    assert "click" not in look and "evaluate_script" not in look


def test_chrome_mode_attaches_and_navigation_asks() -> None:
    entry = browser.build_server_entry(browser.BrowserSettings(mode="chrome"))
    assert "--autoConnect" in entry["args"]
    assert not any(a.startswith("--userDataDir") for a in entry["args"])
    assert "navigate_page" not in entry["annotations"]
    assert "take_snapshot" in entry["annotations"]


def test_executable_and_patterns_become_flags() -> None:
    settings = browser.BrowserSettings(
        executable="/opt/brave", allowed=["*://a.com/*"], blocked=["*://b.com/*"]
    )
    args = browser.build_server_entry(settings)["args"]
    assert "--executablePath=/opt/brave" in args
    assert "--allowedUrlPattern=*://a.com/*" in args
    assert "--blockedUrlPattern=*://b.com/*" in args


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "*://example.com/*"),
        ("*.corp.example", "*://*.corp.example/*"),
        ("https://intranet:*/*", "https://intranet:*/*"),
    ],
)
def test_normalize_pattern(raw: str, expected: str) -> None:
    assert browser.normalize_pattern(raw) == expected


def test_normalize_pattern_rejects_paths() -> None:
    with pytest.raises(ValueError):
        browser.normalize_pattern("example.com/login")


def test_save_keeps_other_servers_toggles_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "mcps.json"
    path.write_text(json.dumps({"mcpServers": {"github": {"command": "x"}}}))
    browser.save_settings(path, browser.BrowserSettings())
    data = json.loads(path.read_text())
    data["mcpServers"]["chrome-devtools"]["tools"]["click"] = False  # user toggle
    path.write_text(json.dumps(data))

    browser.save_settings(path, browser.BrowserSettings(mode="chrome"), enabled=False)

    data = json.loads(path.read_text())
    assert data["mcpServers"]["github"] == {"command": "x"}
    entry = data["mcpServers"]["chrome-devtools"]
    assert entry["tools"]["click"] is False and entry["enabled"] is False
    assert browser.current_settings(path).mode == "chrome"
    assert not browser.is_enabled(path)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert browser.remove(path) and browser.current_settings(path) is None


def test_node_status(monkeypatch) -> None:
    monkeypatch.setattr(browser.shutil, "which", lambda _n: None)
    ok, message = browser.node_status()
    assert not ok and "nodejs.org" in message

    monkeypatch.setattr(browser.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="v18.20.0\n"),
    )
    assert browser.node_status()[0] is False
    monkeypatch.setattr(
        browser.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="v22.11.0\n")
    )
    assert browser.node_status() == (True, "Node.js v22.11.0")


def test_detect_browser_prefers_the_first_installed(
    tmp_path: Path, monkeypatch
) -> None:
    brave = tmp_path / "brave"
    brave.write_text("")
    monkeypatch.setattr(
        browser,
        "_candidate_browsers",
        lambda: [("Chrome", str(tmp_path / "missing")), ("Brave", str(brave))],
    )
    assert browser.detect_browser() == ("Brave", str(brave))


# ─── /browser ────────────────────────────────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _handler(tmp_path: Path, monkeypatch):
    rebuilds: list[str] = []

    async def set_model(model):
        rebuilds.append(model)

    config = SimpleNamespace(mcp_config_file=tmp_path / "mcps.json", enable_mcp=False)
    handler = CommandHandler.__new__(CommandHandler)
    handler.session = SimpleNamespace(
        config=config, current_model="m", set_model=set_model
    )
    handler.host = _Host()
    monkeypatch.setattr("phoson_cli.commands.save_config", lambda *a, **k: None)
    return handler, config, rebuilds


async def _browser(handler, args: str = "") -> None:
    await handler._cmd_browser(Command(name="/browser", args=args))


@pytest.mark.asyncio
async def test_browser_setup_status_and_changes(tmp_path: Path, monkeypatch) -> None:
    handler, config, rebuilds = _handler(tmp_path, monkeypatch)
    await _browser(handler)
    assert "not set up" in handler.host.info[-1]

    monkeypatch.setattr(browser, "node_status", lambda: (True, "Node.js v22.0.0"))
    monkeypatch.setattr(browser, "detect_browser", lambda: ("Chrome", "/x/chrome"))
    await _browser(handler, "setup")
    assert config.enable_mcp is True and rebuilds == ["m"]
    assert "set up with Chrome" in handler.host.info[-1]
    settings = browser.current_settings(config.mcp_config_file)
    assert settings.mode == "profile" and settings.executable is None

    await _browser(handler, "block ads.example")
    await _browser(handler, "mode chrome")
    assert "chrome://inspect" in handler.host.info[-1]
    await _browser(handler, "status")
    status = handler.host.info[-1]
    assert "mode chrome" in status and "*://ads.example/*" in status

    await _browser(handler, "off")
    assert not browser.is_enabled(config.mcp_config_file)
    await _browser(handler, "mode nonsense")
    assert handler.host.errors[-1].startswith("Usage: /browser")


@pytest.mark.asyncio
async def test_browser_setup_explains_missing_node(tmp_path: Path, monkeypatch) -> None:
    handler, _config, rebuilds = _handler(tmp_path, monkeypatch)
    monkeypatch.setattr(browser, "node_status", lambda: (False, "Install Node.js"))
    await _browser(handler, "setup")
    assert handler.host.errors[-1] == "Install Node.js" and rebuilds == []


@pytest.mark.asyncio
async def test_non_chrome_browsers_are_passed_as_executable(
    tmp_path: Path, monkeypatch
) -> None:
    handler, config, _rebuilds = _handler(tmp_path, monkeypatch)
    monkeypatch.setattr(browser, "node_status", lambda: (True, "Node.js v22.0.0"))
    monkeypatch.setattr(browser, "detect_browser", lambda: ("Brave", "/opt/brave"))
    await _browser(handler, "setup")
    assert browser.current_settings(config.mcp_config_file).executable == "/opt/brave"
    assert "officially supported" in handler.host.info[-1]


# ─── MCP annotation overrides ────────────────────────────────────────────────


def test_mcp_annotation_overrides_come_from_the_server_entry() -> None:
    from phoson_plugin_mcp._plugin import MCPPlugin

    plugin = MCPPlugin()
    plugin.servers = {
        "chrome-devtools": {
            "annotations": {"take_snapshot": {"readOnlyHint": True, "bogus": 1}}
        }
    }
    remote = [
        SimpleNamespace(
            name="take_snapshot",
            description="d",
            inputSchema={},
            annotations=SimpleNamespace(readOnlyHint=False, destructiveHint=False),
        ),
        SimpleNamespace(
            name="click", description="d", inputSchema={}, annotations=None
        ),
    ]
    tools = {
        t.name: t
        for t in plugin._agent_tools_from_remote_tools("chrome-devtools", remote)
    }
    snap = tools["mcp_chrome-devtools_take_snapshot"].metadata["mcp_annotations"]
    assert snap["read_only"] is True and snap["destructive"] is False
    click = tools["mcp_chrome-devtools_click"].metadata["mcp_annotations"]
    assert click == {"annotated": False}
