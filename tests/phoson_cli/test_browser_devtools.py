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
    for var in ("USAGE_STATISTICS", "UPDATE_CHECKS", "CONFIG_DISCOVERY"):
        assert entry["env"][f"CHROME_DEVTOOLS_MCP_NO_{var}"] == "1"
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


@pytest.mark.asyncio
async def test_tool_discovery_never_writes_server_stderr_to_the_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    """A stdio server's stderr during discovery goes to its log file: on the
    terminal it tore the full-screen TUI (chrome-devtools-mcp prints notices
    on start)."""
    import contextlib

    from phoson_plugin_mcp import _plugin as mcp

    if not mcp.MCP_AVAILABLE:
        pytest.skip("mcp SDK not installed")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mcp.Path, "home", classmethod(lambda cls: tmp_path))
    seen: dict = {}

    @contextlib.asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        seen["errlog"] = errlog
        raise RuntimeError("stop here")
        yield  # pragma: no cover

    monkeypatch.setattr(mcp, "stdio_client", fake_stdio_client)
    plugin = mcp.MCPPlugin()
    with pytest.raises(RuntimeError):
        await plugin._list_stdio_tools("chrome-devtools", {"command": "npx"})
    errlog = seen["errlog"]
    assert errlog is not None and errlog is not sys.stderr
    assert Path(errlog.name).parent == tmp_path / ".phoson" / "logs" / "mcp"
    plugin._close_errlogs()


def test_entry_saves_round_trips_and_image_tokens() -> None:
    entry = browser.build_server_entry(browser.BrowserSettings())
    args = entry["args"]
    assert "--no-page-id-routing" in args
    assert "--screenshotFormat=jpeg" in args
    assert "--screenshotMaxWidth=1280" in args
    assert "includeSnapshot=true" in entry["hints"]["click"]
    assert "fill_form" in entry["hints"]["fill"]
    assert "evaluate_script" in entry["hints"]


def test_mcp_description_hints_are_appended() -> None:
    from phoson_plugin_mcp._plugin import MCPPlugin

    plugin = MCPPlugin()
    plugin.servers = {"s": {"hints": {"click": "  Use includeSnapshot. ", "x": 3}}}
    remote = [
        SimpleNamespace(name=name, description="Clicks.", inputSchema={})
        for name in ("click", "x", "hover")
    ]
    tools = {
        t.name: t.description
        for t in plugin._agent_tools_from_remote_tools("s", remote)
    }
    assert tools["mcp_s_click"] == "Clicks.\n\nUse includeSnapshot."
    assert tools["mcp_s_x"] == "Clicks."  # a non-string hint is ignored
    assert tools["mcp_s_hover"] == "Clicks."


def _snapshot_result(body: str, extra: str = "\n## Pages\n1: a [selected]") -> str:
    text = f"# click response\nClicked\n{browser.SNAPSHOT_MARKER}\n{body}{extra}"
    return json.dumps(
        {"success": True, "result": [{"type": "text", "text": text}]},
        ensure_ascii=True,
    )


def test_elide_snapshot_keeps_the_rest_of_the_result() -> None:
    raw = _snapshot_result('uid=1_0 RootWebArea "say \\"hi\\""\n  uid=1_1 button')
    text = json.loads(browser.elide_snapshot(raw))["result"][0]["text"]
    assert "uid=1_" not in text and browser.SNAPSHOT_MARKER not in text
    assert text.startswith("# click response\nClicked\n## Page snapshot elided")
    assert text.endswith("## Pages\n1: a [selected]")
    at_end = json.loads(browser.elide_snapshot(_snapshot_result("uid=2_0 x", "")))
    assert "uid=2_0" not in at_end["result"][0]["text"]
    assert browser.elide_snapshot("no snapshot") == "no snapshot"


@pytest.mark.asyncio
async def test_only_the_newest_snapshot_stays_in_the_conversation() -> None:
    from phoson_llm.schemas import Message, TextBlock, ModelConfig, ToolResultBlock

    def result(call: str, body: str) -> Message:
        return Message(
            role="user",
            content=[ToolResultBlock(tool_call_id=call, result=_snapshot_result(body))],
        )

    messages = [
        Message(role="user", content="open the page"),
        result("a", "uid=1_0 old"),
        Message(role="assistant", content=[TextBlock(text="ok")]),
        result("b", "uid=2_0 newer"),
        result("c", "uid=3_0 newest"),
    ]
    middleware = browser.StaleSnapshotMiddleware()
    out = await middleware.on_before_llm(messages, ModelConfig(model="m"))

    def text(message: Message) -> str:
        return message.content[0].result  # type: ignore[union-attr,index]

    assert "uid=1_0" not in text(out[1]) and "uid=2_0" not in text(out[3])
    assert "uid=3_0 newest" in text(out[4])
    assert out[0] is messages[0] and out[2] is messages[2] and out[4] is messages[4]
    assert "uid=1_0" in text(messages[1])  # the input list is not mutated
    again = await middleware.on_before_llm(out, ModelConfig(model="m"))
    assert again is out  # one snapshot left: nothing to do
    single = messages[:2]
    assert await middleware.on_before_llm(single, ModelConfig(model="m")) is single


def test_mcp_image_content_reaches_the_model_as_an_image() -> None:
    from phoson_agent.models import ImageToolResult
    from phoson_plugin_mcp._plugin import _with_image

    payload = {
        "success": True,
        "result": [
            {"type": "text", "text": "Took a screenshot."},
            {"type": "image", "data": "QUJD", "mimeType": "image/jpeg"},
            {"type": "image", "data": "REVG", "mimeType": "image/png"},
        ],
        "tool": "take_screenshot",
        "server": "chrome-devtools",
    }
    out = _with_image(payload)
    assert isinstance(out, ImageToolResult)
    assert out.image.source == "data:image/jpeg;base64,QUJD"
    assert out.image.media_type == "image/jpeg"
    text = json.loads(out.text)
    assert "QUJD" not in out.text and "REVG" not in out.text
    assert text["result"][0] == {"type": "text", "text": "Took a screenshot."}
    assert "attached below" in text["result"][1]["text"]
    assert "omitted" in text["result"][2]["text"]
    plain = {"success": True, "result": [{"type": "text", "text": "x"}]}
    assert _with_image(plain) is plain
