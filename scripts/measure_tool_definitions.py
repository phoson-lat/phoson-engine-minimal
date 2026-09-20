#!/usr/bin/env python
"""Measure tool-definition token cost per run (issue #148 — "medir primero").

Three typical configurations, each run through a *real* AgentEngine with a
fake chat (no network) and the *real* OTel plugin; the report reads the
``phoson.tool_count`` / ``phoson.tool_definitions_tokens`` attributes the
engine records on every run span (issue #140 instrumentation).

Configurations
--------------
  A  sin MCP                     — base CLI tools only
  B  con 2 MCP                   — base + filesystem + memory (real stdio
                                   servers discovered by the real MCP plugin)
  C  con MCP + plugins + skills  — base + ``skill`` tool + 2 MCP + plugins

Usage
-----
    .venv/bin/python scripts/measure_tool_definitions.py

Writes the report to ``bench/results/tool_budget/report.md`` and prints it.
"""

import sys
import json
import asyncio
from pathlib import Path
from collections.abc import AsyncIterator

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from phoson_cli.tools import build_tools  # noqa: E402
from phoson_plugin_mcp import MCPPlugin  # noqa: E402
from phoson_agent.agent import AgentEngine  # noqa: E402
from phoson_llm.schemas import (  # noqa: E402
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolDefinition,
)
from phoson_plugin_otel import PhosonOtelPlugin  # noqa: E402
from phoson_llm.chats.base import BaseLLMChat  # noqa: E402
from phoson_agent.plugins.summarizer import TokenEstimator  # noqa: E402

PY = REPO / ".venv" / "bin" / "python"
SERVERS = REPO / "scripts" / "measure_mcp"
OUT = REPO / "bench" / "results" / "tool_budget"

# Reference context window (Claude-class models) for the % column.
CONTEXT_WINDOW = 200_000


class FinalAnswerChat(BaseLLMChat):
    """One LLM call, final answer: no tool execution needed to measure."""

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=40, output=12),
            cost_usd=0.0001,
            cost_known=True,
        )
        yield LLMDoneEvent(content="ok", has_tool_calls=False)


FS_SERVER = {
    "transport": "stdio",
    "command": str(PY),
    "args": [str(SERVERS / "fs_server.py")],
}
MEMORY_SERVER = {
    "transport": "stdio",
    "command": str(PY),
    "args": [str(SERVERS / "memory_server.py")],
}


def mcp_plugin(servers: dict) -> MCPPlugin:
    plugin = MCPPlugin()
    plugin.configure({"servers": servers})
    return plugin


async def measure(
    label: str,
    tools: list,
    extra_plugins: list,
    trace_file: Path,
) -> dict:
    """Run one trivial prompt through a real engine and read the run span."""
    otel = PhosonOtelPlugin()
    otel.configure({"service_name": "tool-budget", "file_path": str(trace_file)})
    engine = AgentEngine(
        chat=FinalAnswerChat(),
        tools=tools,
        plugins=[otel, *extra_plugins],
        max_iterations=2,
    )
    try:
        await engine.run(
            [Message(role="user", content="ping")],
            ModelConfig(model="measure"),
        )
    finally:
        for plugin in extra_plugins:
            aclose = getattr(plugin, "aclose", None)
            if aclose is not None:
                await aclose()

    doc = json.loads(trace_file.read_text(encoding="utf-8"))
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    run_span = next(s for s in spans if s["name"] == "phoson.run")
    attrs = {a["key"]: a["value"] for a in run_span["attributes"]}
    return {
        "label": label,
        "tool_count": int(attrs["phoson.tool_count"]["intValue"]),
        "tokens": int(attrs["phoson.tool_definitions_tokens"]["intValue"]),
    }


def group_of(tool) -> str:
    name = tool.name
    if name.startswith("mcp_filesystem_"):
        return "MCP · filesystem"
    if name.startswith("mcp_memory_"):
        return "MCP · memory"
    if name == "skill":
        return "Skills"
    return "Base"


def breakdown(estimator: TokenEstimator, tools: list) -> dict[str, tuple[int, int]]:
    """Per-group (tool count, token weight) using the canonical serializer."""
    out: dict[str, dict] = {}
    for tool in tools:
        group = group_of(tool)
        td = ToolDefinition(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
        )
        tokens = estimator.count_tools([td])
        n, total = out.get(group, (0, 0))
        out[group] = (n + 1, total + tokens)
    return {g: (n, t) for g, (n, t) in out.items()}


ORDER = ["Base", "Skills", "MCP · filesystem", "MCP · memory"]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    estimator = TokenEstimator("openrouter")
    results: list[dict] = []

    # ── Config A: sin MCP ─────────────────────────────────────────────
    tools_a = build_tools()
    r = await measure("A · sin MCP", tools_a, [], OUT / "trace_A.json")
    r["breakdown"] = breakdown(estimator, tools_a)
    results.append(r)

    # ── Config B: con 2 MCP ───────────────────────────────────────────
    mcp_b = mcp_plugin({"filesystem": FS_SERVER, "memory": MEMORY_SERVER})
    tools_b = build_tools()
    r = await measure("B · con 2 MCP", tools_b, [mcp_b], OUT / "trace_B.json")
    r["breakdown"] = breakdown(estimator, tools_b + list(mcp_b.tools_cache))
    results.append(r)

    # ── Config C: con MCP + plugins + skills ──────────────────────────
    mcp_c = mcp_plugin({"filesystem": FS_SERVER, "memory": MEMORY_SERVER})
    tools_c = build_tools(include_skill=True)
    r = await measure(
        "C · MCP + plugins + skills",
        tools_c,
        [mcp_c],
        OUT / "trace_C.json",
    )
    full_c = tools_c + list(mcp_c.tools_cache)
    r["breakdown"] = breakdown(estimator, full_c)
    results.append(r)

    # ── Report ────────────────────────────────────────────────────────
    lines = [
        "# Reporte: tokens de definiciones de tools por run (issue #148)",
        "",
        "Medidos con `AgentEngine` real + plugin OTel real (issue #140): el",
        "run-span lleva `phoson.tool_count` y `phoson.tool_definitions_tokens`.",
        "Chat simulado (sin red); 1 LLM call por run. Estimator: tiktoken",
        "cl100k_base (misma vía que la puerta de auto-compact).",
        "",
        "| Config | Tools | Tokens definiciones | % ventana 200k |",
        "|---|---|---|---|",
    ]
    for r in results:
        pct = 100.0 * r["tokens"] / CONTEXT_WINDOW
        lines.append(
            f"| {r['label']} | {r['tool_count']} | {r['tokens']:,} | {pct:.1f}% |"
        )

    lines += [
        "",
        "## Desglose por grupo",
        "",
    ]
    for r in results:
        lines.append(f"### {r['label']}")
        lines.append("")
        lines.append("| Grupo | Tools | Tokens |")
        lines.append("|---|---|---|")
        for group in ORDER:
            if group in r["breakdown"]:
                n, t = r["breakdown"][group]
                lines.append(f"| {group} | {n} | {t:,} |")
        lines.append("")

    # Per-tool detail: the full catalog of config C (heaviest case).
    lines += [
        "## Catalogo completo (config C) — tokens por tool",
        "",
        "| Tool | Tokens |",
        "|---|---|",
    ]

    def _td(tool) -> ToolDefinition:
        return ToolDefinition(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
        )

    for tool in sorted(full_c, key=lambda t: -estimator.count_tools([_td(t)])):
        lines.append(f"| {tool.name} | {estimator.count_tools([_td(tool)]):,} |")
    lines.append("")

    report = "\n".join(lines)
    (OUT / "report.md").write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\n(traces guardados en {OUT})")


if __name__ == "__main__":
    asyncio.run(main())
