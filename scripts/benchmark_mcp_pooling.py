#!/usr/bin/env python3
"""Benchmark: MCP session pooling vs. reconnecting on every tool call.

Runs N successive calls to the same tool on a real (subprocess-based) MCP
server, under two strategies:

  - "unpooled": reconnects from scratch (spawn subprocess, handshake,
    initialize, list_tools) on every single call — this is what
    phoson_plugin_mcp did before this fix.
  - "pooled": phoson_plugin_mcp.MCPPlugin as it works today — one
    connection reused across all N calls.

No network/npm dependency: the server is
``tests/phoson_plugin_mcp/fixtures/echo_server.py``, a tiny pure-Python
FastMCP stdio server.

Usage:
    python scripts/benchmark_mcp_pooling.py [--calls N]
"""

import sys
import time
import asyncio
import argparse
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from phoson_plugin_mcp import MCPPlugin  # noqa: E402

ECHO_SERVER = str(
    Path(__file__).resolve().parent.parent
    / "tests"
    / "phoson_plugin_mcp"
    / "fixtures"
    / "echo_server.py"
)
SERVER_PARAMS = StdioServerParameters(command=sys.executable, args=[ECHO_SERVER])


async def run_unpooled(n_calls: int) -> float:
    """Reconnect (spawn + handshake + list_tools) on every call — the old behavior."""
    start = time.perf_counter()
    for i in range(n_calls):
        async with stdio_client(SERVER_PARAMS) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                await session.call_tool("echo", {"text": f"call-{i}"})
    return time.perf_counter() - start


async def run_pooled(n_calls: int) -> float:
    """Reuse one pooled session across all calls — current phoson_plugin_mcp."""
    plugin = MCPPlugin()
    plugin.servers = {"echo": {"command": sys.executable, "args": [ECHO_SERVER]}}
    try:
        start = time.perf_counter()
        for i in range(n_calls):
            result = await plugin._execute_mcp_tool(
                "echo", "echo", {"text": f"call-{i}"}
            )
            assert result.get("success"), result
        return time.perf_counter() - start
    finally:
        await plugin.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=10)
    args = parser.parse_args()

    print(f"Benchmarking {args.calls} successive calls to the same MCP tool...\n")

    unpooled_total = await run_unpooled(args.calls)
    pooled_total = await run_pooled(args.calls)

    print(f"{'strategy':<12} {'total (s)':>12} {'avg/call (ms)':>16}")
    print(
        f"{'unpooled':<12} {unpooled_total:>12.4f} "
        f"{unpooled_total / args.calls * 1000:>16.2f}"
    )
    print(
        f"{'pooled':<12} {pooled_total:>12.4f} "
        f"{pooled_total / args.calls * 1000:>16.2f}"
    )

    speedup = unpooled_total / pooled_total if pooled_total > 0 else float("inf")
    print(f"\npooled is {speedup:.1f}x faster over {args.calls} successive calls")


if __name__ == "__main__":
    asyncio.run(main())
