"""End-to-end pooling tests against a real MCP stdio server (subprocess).

Uses ``tests/phoson_plugin_mcp/fixtures/echo_server.py`` — a tiny pure-Python
FastMCP server — so these tests exercise a real spawn/handshake/call cycle
without any network dependency (no npx/node servers needed).
"""

import sys
from pathlib import Path

import pytest

from phoson_plugin_mcp import MCPPlugin

try:
    import mcp  # noqa: F401

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not MCP_AVAILABLE, reason="mcp package not installed (pip install mcp)"
)

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_server.py")


@pytest.fixture
def plugin():
    p = MCPPlugin()
    p.servers = {"echo": {"command": sys.executable, "args": [ECHO_SERVER]}}
    yield p


@pytest.mark.asyncio
async def test_first_call_opens_and_caches_a_session(plugin):
    assert "echo" not in plugin.sessions

    result = await plugin._execute_mcp_tool("echo", "echo", {"text": "hi"})

    assert result["success"] is True
    assert result["result"][0]["text"] == "hi"
    assert "echo" in plugin.sessions
    await plugin.aclose()


@pytest.mark.asyncio
async def test_successive_calls_reuse_the_same_subprocess(plugin):
    """The core pooling claim: two calls, one live subprocess.

    Uses the server's own uptime-since-start tool: if pooling works, the
    second call observes strictly more uptime than the first (same
    process, clock kept running). If each call spawned a fresh process,
    both would report ~0 with no guaranteed ordering.
    """
    try:
        first = await plugin._execute_mcp_tool("echo", "slow_connect_marker", {})
        second = await plugin._execute_mcp_tool("echo", "slow_connect_marker", {})

        uptime_1 = float(first["result"][0]["text"])
        uptime_2 = float(second["result"][0]["text"])

        assert uptime_2 > uptime_1 >= 0
        # Exactly one session was ever created for this server.
        assert list(plugin.sessions.keys()) == ["echo"]
    finally:
        await plugin.aclose()


@pytest.mark.asyncio
async def test_tool_list_is_fetched_once_not_per_call(plugin, monkeypatch):
    calls = {"count": 0}

    try:
        session = await plugin._get_session("echo")
        real_list_tools = session.list_tools

        async def counting_list_tools():
            calls["count"] += 1
            return await real_list_tools()

        monkeypatch.setattr(session, "list_tools", counting_list_tools)

        await plugin._call_tool_on_cached_session(
            session, "echo", "echo", {"text": "a"}
        )
        await plugin._call_tool_on_cached_session(
            session, "echo", "echo", {"text": "b"}
        )
        await plugin._call_tool_on_cached_session(
            session, "echo", "echo", {"text": "c"}
        )

        assert calls["count"] == 1
    finally:
        await plugin.aclose()


class _BrokenSession:
    """Stands in for a session whose underlying connection died."""

    async def call_tool(self, *args, **kwargs):
        raise ConnectionError("simulated broken pipe")


@pytest.mark.asyncio
async def test_broken_session_self_heals_on_next_call(plugin):
    """If the cached session goes bad, the plugin drops it and reconnects
    instead of failing forever."""
    await plugin._execute_mcp_tool("echo", "echo", {"text": "warm-up"})
    assert "echo" in plugin.sessions

    # Simulate a session that's present but whose connection died.
    plugin.sessions["echo"] = _BrokenSession()

    result = await plugin._execute_mcp_tool("echo", "echo", {"text": "after-break"})

    assert "error" in result
    assert "echo" not in plugin.sessions  # dropped, ready to reconnect

    recovered = await plugin._execute_mcp_tool("echo", "echo", {"text": "recovered"})
    assert recovered["success"] is True
    await plugin.aclose()


@pytest.mark.asyncio
async def test_aclose_tears_down_pooled_connections(plugin):
    await plugin._execute_mcp_tool("echo", "echo", {"text": "hi"})
    assert plugin.sessions

    await plugin.aclose()

    assert plugin.sessions == {}
    assert plugin._server_tool_lists == {}
