"""Host side of the ``tools`` namespace a code-mode script sees.

:class:`ToolBridge` decides which registered tools a script may call, how
they are named in Python, and executes each call the worker relays —
through :meth:`ToolRunner.admit_nested` / :meth:`ToolRunner.invoke_nested`,
so the permission gate applies to every one of them exactly as it does to
a model-issued call.
"""

import re
import json
import uuid
import asyncio
import keyword
from typing import TYPE_CHECKING, Any
from collections import Counter

from phoson_llm.schemas import ToolCallEvent
from phoson_agent.models import (
    CODE_MODE_KEY,
    CODE_RESULT_KEY,
    OUTPUT_SCHEMA_KEY,
    AgentTool,
    ImageToolResult,
)
from phoson_agent.tool_discovery import _score, _tokens

if TYPE_CHECKING:
    from phoson_agent._tool_runner import ToolRunner

RUN_CODE_TOOL_NAME = "run_code"

#: Tools never exposed to scripts: code mode itself, tools whose result only
#: makes sense to the model (discovery reveals, context compaction) and
#: sub-agent spawners (a script fanning out agents is a cost bomb).
DEFAULT_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {RUN_CODE_TOOL_NAME, "discover", "compact_context", "agent", "agents"}
)

#: Attribute names the worker's ``tools`` object already uses.
_RESERVED = frozenset({"call", "search", "help", "list"})

_JSON_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}

_DOC_LIMIT = 600


def _py_identifier(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    if keyword.iskeyword(ident) or ident in _RESERVED:
        ident += "_tool"
    return ident


def _type_hint(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "Any"
    kind = schema.get("type")
    if isinstance(kind, list):
        names = [_JSON_TO_PY.get(k, "Any") for k in kind if isinstance(k, str)]
        return " | ".join(dict.fromkeys(names)) or "Any"
    if isinstance(kind, str):
        return _JSON_TO_PY.get(kind, "Any")
    if "enum" in schema:
        return "str"
    return "Any"


def _ordered_params(parameters: Any) -> tuple[list[str], list[str]]:
    """``(all_params_in_call_order, required)``: required ones first."""
    if not isinstance(parameters, dict):
        return [], []
    props = parameters.get("properties")
    if not isinstance(props, dict):
        return [], []
    required = [r for r in parameters.get("required") or [] if r in props]
    optional = [p for p in props if p not in required]
    return required + optional, required


def signature(py_name: str, parameters: Any) -> str:
    """Python-style one-line signature, e.g. ``grep(pattern: str, path: str = ...)``."""
    order, required = _ordered_params(parameters)
    props = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    parts = []
    for name in order:
        hint = _type_hint(props.get(name))
        parts.append(f"{name}: {hint}" if name in required else f"{name}: {hint} = ...")
    return f"{py_name}({', '.join(parts)})"


def _short_doc(description: str, limit: int = _DOC_LIMIT) -> str:
    text = " ".join((description or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def script_value(tool: AgentTool | None, raw: Any) -> Any:
    """The value a script receives for a handler's raw return.

    Structured results stay structured: a ``dict`` is passed through, a
    string holding a JSON object/array is decoded, and a tool may install
    its own converter under ``metadata[CODE_RESULT_KEY]`` (MCP does, to
    unwrap ``structuredContent``). Anything else is the plain text.
    """
    hook = tool.metadata.get(CODE_RESULT_KEY) if tool is not None else None
    if callable(hook):
        return hook(raw)
    if isinstance(raw, ImageToolResult):
        return raw.text
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped[:1] in ("{", "["):
            try:
                return json.loads(stripped)
            except ValueError:
                pass
        return raw
    return raw


class ToolBridge:
    """Executes the tool calls one script makes; one instance per run."""

    def __init__(
        self,
        runner: "ToolRunner",
        tools: dict[str, AgentTool],
        *,
        max_calls: int,
    ) -> None:
        self._runner = runner
        self._tools = tools
        self._max_calls = max_calls
        self._py_names = assign_python_names(tools)
        # Admissions (permission prompts) are strictly one at a time; tools
        # that are not parallel-safe also run one at a time, like they do in
        # a model turn.
        self._admit_lock = asyncio.Lock()
        self._serial_lock = asyncio.Lock()
        self.calls: list[tuple[str, bool]] = []

    # ── description helpers ──────────────────────────────────────────

    def specs(self) -> list[dict[str, Any]]:
        """Tool specs sent to the worker to build ``tools.<name>`` stubs."""
        out = []
        for name, tool in self._tools.items():
            order, required = _ordered_params(tool.parameters)
            py_name = self._py_names[name]
            out.append(
                {
                    "name": name,
                    "py": py_name,
                    "params": order,
                    "required": required,
                    "doc": signature(py_name, tool.parameters)
                    + "\n\n"
                    + (tool.description or ""),
                }
            )
        return out

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 10
        query_tokens = _tokens(str(query or ""))
        scored = [
            (_score(query_tokens, tool), name) for name, tool in self._tools.items()
        ]
        hits = sorted(
            ((s, n) for s, n in scored if s > 0), key=lambda p: (-p[0], p[1])
        )[:limit]
        return [self.summary(name) for _, name in hits]

    def summary(self, name: str) -> dict[str, Any]:
        tool = self._tools[name]
        py_name = self._py_names[name]
        return {
            "name": name,
            "py": py_name,
            "signature": "tools." + signature(py_name, tool.parameters),
            "description": _short_doc(tool.description),
        }

    def help(self, name: str) -> str:
        tool = self._resolve(name)
        if tool is None:
            raise LookupError(f"no tool named {name!r}")
        py_name = self._py_names[tool.name]
        lines = [
            "tools." + signature(py_name, tool.parameters),
            f"(registered name: {tool.name})",
            "",
            tool.description or "",
            "",
            "Parameters (JSON Schema):",
            json.dumps(tool.parameters, indent=2, ensure_ascii=False),
        ]
        output_schema = tool.metadata.get(OUTPUT_SCHEMA_KEY)
        if isinstance(output_schema, dict):
            lines += [
                "",
                "Returns (JSON Schema):",
                json.dumps(output_schema, indent=2, ensure_ascii=False),
            ]
        return "\n".join(lines)

    def _resolve(self, name: str) -> AgentTool | None:
        tool = self._tools.get(name)
        if tool is not None:
            return tool
        for registered, py_name in self._py_names.items():
            if py_name == name:
                return self._tools[registered]
        return None

    # ── execution ────────────────────────────────────────────────────

    async def call(self, name: str, args: Any) -> tuple[bool, Any]:
        """Run one call; ``(True, value)`` or ``(False, error_message)``."""
        tool = self._resolve(name)
        if tool is None:
            self.calls.append((name, False))
            return False, (
                f"no tool named {name!r} is available to scripts; use "
                "tools.search('keyword') to find one"
            )
        if len(self.calls) >= self._max_calls:
            return False, (
                f"tool call limit reached ({self._max_calls} per run_code call); "
                "split the work across several run_code calls"
            )
        if not isinstance(args, dict):
            self.calls.append((tool.name, False))
            return False, "tool arguments must be keyword arguments"

        call = ToolCallEvent(
            index=0,
            tool_call_id=f"code_{uuid.uuid4().hex[:12]}",
            tool_name=tool.name,
            args=args,
        )
        async with self._admit_lock:
            admission = await self._runner.admit_nested(call)
        if not isinstance(admission, ToolCallEvent):
            self.calls.append((tool.name, False))
            return False, admission.text

        if tool.parallel_safe:
            result = await self._runner.invoke_nested(admission)
        else:
            async with self._serial_lock:
                result = await self._runner.invoke_nested(admission)
        self.calls.append((tool.name, result.ok))
        if not result.ok:
            return False, result.text
        try:
            return True, script_value(result.tool or tool, result.raw)
        except Exception as exc:  # noqa: BLE001 - a converter may reject a result
            return False, str(exc) or type(exc).__name__

    def call_summary(self) -> str:
        """``[run_code: 3 tool calls — read_file×2, grep×1; 1 failed]``."""
        if not self.calls:
            return ""
        counts = Counter(name for name, _ in self.calls)
        failed = sum(1 for _, ok in self.calls if not ok)
        parts = ", ".join(f"{name}×{n}" for name, n in counts.most_common(8))
        if len(counts) > 8:
            parts += ", …"
        tail = f"; {failed} failed" if failed else ""
        total = len(self.calls)
        noun = "tool call" if total == 1 else "tool calls"
        return f"[run_code: {total} {noun} — {parts}{tail}]"


def callable_from_code(tool: AgentTool, excluded: frozenset[str]) -> bool:
    return tool.name not in excluded and tool.metadata.get(CODE_MODE_KEY) is not False


def assign_python_names(tools: dict[str, AgentTool]) -> dict[str, str]:
    """Registered name → unique Python identifier."""
    used: set[str] = set()
    out: dict[str, str] = {}
    for name in tools:
        base = _py_identifier(name)
        candidate = base
        n = 2
        while candidate in used:
            candidate = f"{base}_{n}"
            n += 1
        used.add(candidate)
        out[name] = candidate
    return out


__all__ = [
    "DEFAULT_EXCLUDED_TOOLS",
    "RUN_CODE_TOOL_NAME",
    "ToolBridge",
    "assign_python_names",
    "callable_from_code",
    "script_value",
    "signature",
]
