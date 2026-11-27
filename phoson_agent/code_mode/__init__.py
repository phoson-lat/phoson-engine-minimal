"""Code mode: the model writes a script that calls tools as functions.

Instead of one tool call per LLM step, the model sends a Python script to
the ``run_code`` tool. The script calls ``tools.<name>(...)``, loops,
filters and aggregates; only what it ``print()``s returns to the model's
context. Long chains of calls cost one model turn, and bulky intermediate
results never enter the context window.

It is provider-agnostic: ``run_code`` is an ordinary function tool, so it
works with every chat adapter (compare Anthropic's server-side
"programmatic tool calling" or Cloudflare's "Code Mode").

Safety model:

* every ``tools.*`` call a script makes goes through the engine's
  ``on_before_tool`` chain — the permission gate, with its prompt, applies
  per call exactly as for a model-issued call;
* the script runs in a separate process with a minimal environment (API
  keys stay in the host) and, where available, an OS sandbox with no
  network, no writes outside a scratch dir and no access to credential
  stores (see :mod:`._sandbox`);
* ``run_code`` itself carries the ``lang_exec`` intent, so it asks by
  default under the safe permission defaults, like ``bash``.

Enable with ``AgentEngine(code_mode=CodeModeConfig())``.
"""

import math
from typing import TYPE_CHECKING, Any
from dataclasses import field, dataclass

from phoson_agent.models import AgentTool

from ._bridge import (
    RUN_CODE_TOOL_NAME,
    DEFAULT_EXCLUDED_TOOLS,
    ToolBridge,
    signature,
    callable_from_code,
    assign_python_names,
)
from ._runner import WORKER_FLAG, CodeRunError, run_script, worker_command
from ._sandbox import SANDBOX_MODES, SandboxUnavailableError, detect_backend

if TYPE_CHECKING:
    from phoson_agent._tool_runner import ToolRunner

#: Signatures listed in the ``run_code`` description before the rest are
#: left to ``tools.search``.
_MAX_LISTED = 40

#: ``AgentTool.metadata`` marker identifying the run_code tool.
CODE_MODE_FLAG = "code_mode_runner"


@dataclass
class CodeModeConfig:
    """Settings for the ``run_code`` tool.

    Attributes:
        timeout_seconds: Default time limit of one script. Time spent inside
            tool calls (and their permission prompts) does not count.
        max_timeout_seconds: Upper bound for the per-call ``timeout`` arg.
        max_tool_calls: Tool calls one script may make.
        max_output_bytes: Printed output kept for the result.
        sandbox: ``"auto"`` (sandbox when available), ``"required"`` (refuse
            without one) or ``"off"``.
        python: Interpreter command for the worker (e.g. ``["python3"]``);
            ``None`` picks one automatically (see :func:`worker_command`).
        exclude_tools: Extra tool names scripts may not call.
    """

    timeout_seconds: float = 120.0
    max_timeout_seconds: float = 1800.0
    max_tool_calls: int = 200
    max_output_bytes: int = 50 * 1024
    sandbox: str = "auto"
    python: list[str] | None = None
    exclude_tools: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.sandbox not in SANDBOX_MODES:
            raise ValueError(
                f"sandbox must be one of {SANDBOX_MODES}, got {self.sandbox!r}"
            )
        self.exclude_tools = frozenset(self.exclude_tools)

    @property
    def excluded(self) -> frozenset[str]:
        return DEFAULT_EXCLUDED_TOOLS | self.exclude_tools


def _sanitize_timeout(value: Any, config: CodeModeConfig) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return config.timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        return config.timeout_seconds
    return min(timeout, config.max_timeout_seconds)


def _description(
    config: CodeModeConfig,
    listed: list[AgentTool],
    more: int,
    sandboxed: bool,
) -> str:
    py_names = assign_python_names({t.name: t for t in listed})
    lines = [
        "Run a Python 3 script that calls your tools as functions; only what "
        "the script prints comes back to you. Use it to chain many tool calls, "
        "loop over results, filter or aggregate large outputs, or read many "
        "files/pages in parallel — one run_code call instead of many tool "
        "steps. Intermediate results stay inside the script, out of your "
        "context.",
        "",
        "Inside the script:",
        "- `tools.<name>(...)` calls a tool with the arguments of its schema. "
        "It returns a dict/list when the tool produces JSON, otherwise a "
        "string. A failed or refused call raises `ToolError` (catch it to "
        "carry on).",
        "- `parallel(lambda: ..., lambda: ...)` runs calls concurrently and "
        "returns their results in order.",
        "- `tools.search('keyword')` finds more tools (MCP, plugins) and "
        "returns [{name, signature, description}]; `tools.help('name')` "
        "returns the full schema; `tools.call('exact_name', **args)` calls "
        "any of them by registered name.",
        "- Tools that return text give you the same text you would see "
        "(e.g. read_file numbers each line as 'N<TAB>line'; glob, grep and "
        "list_dir put one entry per line): parse it with .splitlines().",
        "- Calls are synchronous: do not use `await`. Standard library only.",
        f"- print() only what you need (counts, matches, short summaries): "
        f"output is capped at {config.max_output_bytes // 1024} KB.",
        "- Each tool call still goes through the user's permission rules.",
    ]
    if sandboxed:
        lines.append(
            "- The script itself is sandboxed: no network access and no file "
            "writes outside its temp dir — do side effects through tools."
        )
    else:
        lines.append(
            "- Do side effects (writing files, fetching URLs) through tools, "
            "not directly from Python, so the user's permission rules apply."
        )
    lines.append(
        f"- Time limit: {config.timeout_seconds:.0f}s by default (`timeout` "
        f"arg, max {config.max_timeout_seconds:.0f}s); time spent inside tool "
        "calls does not count. At most "
        f"{config.max_tool_calls} tool calls per run."
    )
    if listed:
        lines += ["", "Functions:"]
        lines += ["tools." + signature(py_names[t.name], t.parameters) for t in listed]
    if more:
        lines.append(
            f"(+ {more} more tool(s) — find them with tools.search('keyword'))"
        )
    return "\n".join(lines)


def make_run_code_tool(
    *,
    runner: "ToolRunner",
    tools_by_name: dict[str, AgentTool],
    config: CodeModeConfig,
    listed_names: list[str] | None = None,
) -> AgentTool:
    """Build the ``run_code`` tool bound to an engine's runner and registry.

    Args:
        runner: The engine's tool runner (admission + handlers).
        tools_by_name: The engine's live registry; read at call time, so it
            may gain tools after construction.
        config: Code mode settings.
        listed_names: Tools whose signatures the description lists (the
            ones the model already sees); ``None`` lists every callable tool
            up to a cap. The rest are reachable through ``tools.search``.
    """
    excluded = config.excluded
    callable_tools = [
        t for t in tools_by_name.values() if callable_from_code(t, excluded)
    ]
    if listed_names is None:
        listed = callable_tools[:_MAX_LISTED]
    else:
        wanted = set(listed_names)
        listed = [t for t in callable_tools if t.name in wanted][:_MAX_LISTED]
    more = len(callable_tools) - len(listed)
    sandboxed = config.sandbox != "off" and detect_backend() is not None

    async def handler(args: dict[str, Any], _context: Any = None) -> str:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise CodeRunError("run_code needs a non-empty `code` string.")
        tools = {
            name: tool
            for name, tool in tools_by_name.items()
            if callable_from_code(tool, excluded)
        }
        bridge = ToolBridge(runner, tools, max_calls=config.max_tool_calls)
        return await run_script(
            code,
            bridge=bridge,
            config=config,
            timeout=_sanitize_timeout(args.get("timeout"), config),
        )

    return AgentTool(
        name=RUN_CODE_TOOL_NAME,
        description=_description(config, listed, more, sandboxed),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python 3 source. Top-level code runs as a script; "
                        "`tools`, `parallel` and `ToolError` are predefined."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Time limit in seconds (default "
                        f"{config.timeout_seconds:.0f}, max "
                        f"{config.max_timeout_seconds:.0f}); time inside tool "
                        "calls does not count."
                    ),
                },
            },
            "required": ["code"],
        },
        handler=handler,
        metadata={CODE_MODE_FLAG: True},
    )


def worker_entry() -> int:
    """Run the worker in this process (the standalone binary's worker mode)."""
    from . import _worker

    return _worker.main()


__all__ = [
    "RUN_CODE_TOOL_NAME",
    "SANDBOX_MODES",
    "WORKER_FLAG",
    "CodeModeConfig",
    "CodeRunError",
    "SandboxUnavailableError",
    "detect_backend",
    "make_run_code_tool",
    "worker_command",
    "worker_entry",
]
