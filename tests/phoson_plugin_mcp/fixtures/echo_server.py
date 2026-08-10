"""Tiny real MCP stdio server used by pooling tests and the benchmark script.

No network/npm dependency: pure-Python, run as a subprocess via
``python tests/phoson_plugin_mcp/fixtures/echo_server.py``.
"""

import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("phoson-echo-test-server")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the given text."""
    return text


@mcp.tool()
def slow_connect_marker() -> float:
    """Returns the wall-clock time this process has been alive, in seconds.

    Useful to prove a benchmark reused one live process across calls
    instead of spawning a fresh one per call.
    """
    return time.monotonic() - _START


_START = time.monotonic()


if __name__ == "__main__":
    mcp.run(transport="stdio")
