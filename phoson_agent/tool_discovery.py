"""Cache-aware tool discovery (issue #148).

When the registered tool catalog exceeds a token budget, non-core tools
(MCP servers, plugin tools) are *masked*: the model keeps the core tools
plus a single ``discover`` meta-tool and searches for what it needs on
demand.

KV-cache safety (the issue's hard constraint):

* Visibility is **monotonic** — a tool once revealed is never masked
  again, so tool results that reference it stay coherent.
* Order is **stable** — core tools first (constructor order), then
  revealed tools appended in reveal order.  The serialized ``tools``
  payload is therefore always a *prefix extension* of the previous one,
  so the KV cache of everything already sent stays valid.

The weight estimate uses the same canonical JSON + tiktoken path the
auto-compact gate uses (``TokenEstimator.count_tools``), so budget
numbers cannot drift from the CLI's context indicator.
"""

import re
import json
import logging
from typing import Any

from phoson_llm.schemas import ToolDefinition
from phoson_agent.models import AgentTool

logger = logging.getLogger(__name__)

DISCOVER_TOOL_NAME = "discover"
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25


def _to_definition(tool: AgentTool) -> ToolDefinition:
    return ToolDefinition(
        name=tool.name, description=tool.description, parameters=tool.parameters
    )


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z]+", text.lower()) if len(t) >= 2}


def _category(tool_name: str) -> str:
    """Group label for the overview: ``mcp_<server>_*`` → ``mcp_<server>``."""
    parts = tool_name.split("_")
    if len(parts) >= 3 and parts[0] == "mcp":
        return f"{parts[0]}_{parts[1]}"
    return tool_name


def _score(query_tokens: set[str], tool: AgentTool) -> int:
    name = tool.name.lower()
    name_tokens = _tokens(name)
    desc_tokens = _tokens(tool.description or "")
    score = 0
    for q in query_tokens:
        if q in name:
            score += 8
        if q in name_tokens:
            score += 6
        if q in desc_tokens:
            score += 3
    return score


class ToolCatalog:
    """Split the session tool registry into a visible core and a masked tail.

    ``core`` tools are always sent to the model (plus the ``discover``
    meta-tool when active).  ``hidden`` tools start masked and are
    revealed on demand via :meth:`discover`.  When inactive (no budget,
    or the full catalog fits in it), :meth:`definitions` returns the
    complete list in original order and no discover tool exists — the
    pre-#148 behaviour, unchanged.
    """

    def __init__(
        self,
        core: list[AgentTool],
        hidden: list[AgentTool],
        budget_tokens: int | None,
    ) -> None:
        self._core: list[AgentTool] = list(core)
        self._hidden: dict[str, AgentTool] = {
            t.name: t for t in hidden if t.name != DISCOVER_TOOL_NAME
        }
        self._revealed: list[str] = []
        self._budget = budget_tokens
        self._definitions_cache: list[ToolDefinition] | None = None
        self._discover_tool: AgentTool | None = None
        self.active: bool = (
            budget_tokens is not None
            and bool(self._hidden)
            and self._weight(list(self._core) + list(self._hidden.values()))
            > budget_tokens
        )
        if self.active:
            self._discover_tool = _make_discover_tool(self)
            logger.info(
                "Tool catalog over budget (%s tokens): %d tools masked "
                "behind 'discover'.",
                budget_tokens,
                len(self._hidden),
            )

    # ── weights ───────────────────────────────────────────────────────

    @staticmethod
    def _weight(tools: list[AgentTool]) -> int:
        if not tools:
            return 0
        from phoson_agent.plugins.summarizer import TokenEstimator

        return TokenEstimator().count_tools([_to_definition(t) for t in tools])

    def _visible_tools(self) -> list[AgentTool]:
        if not self.active:
            return list(self._core) + list(self._hidden.values())
        out: list[AgentTool] = []
        if self._discover_tool is not None:
            out.append(self._discover_tool)
        out.extend(self._core)
        out.extend(self._hidden[n] for n in self._revealed)
        return out

    def visible_weight(self) -> int:
        """Token weight of the current (visible) ``tools`` payload."""
        return self._weight(self._visible_tools())

    # ── definitions ───────────────────────────────────────────────────

    def definitions(self) -> list[ToolDefinition]:
        """The ``tools`` payload for the next LLM call.

        Re-callable on purpose: the engine passes this bound method
        through the LLM chain so every call re-resolves the (possibly
        grown) visible set.
        """
        if self._definitions_cache is None:
            self._definitions_cache = [_to_definition(t) for t in self._visible_tools()]
        return self._definitions_cache

    def invalidate(self) -> None:
        self._definitions_cache = None

    @property
    def discover_tool(self) -> AgentTool | None:
        """The discover meta-tool (present only when active)."""
        return self._discover_tool

    def visible_tools(self) -> list[AgentTool]:
        """The tools actually sent to the LLM (masked tail excluded)."""
        return self._visible_tools()

    # ── reveal / discover ─────────────────────────────────────────────

    def reveal(self, names: list[str]) -> list[str]:
        """Append-only reveal; returns the names newly revealed, in order."""
        newly: list[str] = []
        for name in names:
            if name in self._hidden and name not in self._revealed:
                self._revealed.append(name)
                newly.append(name)
        if newly:
            self.invalidate()
        return newly

    def hidden_count(self) -> int:
        return len(self._hidden) - len(self._revealed)

    def discover(
        self,
        query: str,
        category: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> str:
        """Search the masked tail and reveal the best matches.

        The returned text is the tool result the model reads: a category
        overview (empty query) or the wire-format JSON of the revealed
        tools.
        """
        if not self.active:
            return (
                "Tool discovery is not active: the full catalog is already "
                "visible, use the tools you have."
            )
        query = (query or "").strip()
        if not query:
            return self._overview()
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))
        query_tokens = _tokens(query)
        scored: list[tuple[int, AgentTool]] = []
        for name, tool in self._hidden.items():
            if name in self._revealed:
                continue
            if category and category.lower() not in name.lower():
                continue
            score = _score(query_tokens, tool)
            if score > 0:
                scored.append((score, tool))
        scored.sort(key=lambda p: (-p[0], p[1].name))
        if not scored:
            return (
                f"No masked tools match {query!r}. Try other keywords or a "
                "full tool name, or call discover with an empty query to "
                "list the available categories."
            )
        top = scored[:limit]
        names = self.reveal([t.name for _, t in top])
        payload = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for _, t in top
        ]
        return (
            f"Revealed {len(names)} tool(s); they can be called starting "
            "from the next step:\n" + json.dumps(payload, ensure_ascii=True)
        )

    def _overview(self) -> str:
        groups: dict[str, int] = {}
        for name in self._hidden:
            if name not in self._revealed:
                cat = _category(name)
                groups[cat] = groups.get(cat, 0) + 1
        lines = ["Masked tool categories (hidden until discovered):"]
        for cat in sorted(groups):
            lines.append(f"  {cat}: {groups[cat]} tool(s)")
        lines.append(
            "Call discover with a keyword or tool name to reveal the matching tools."
        )
        return "\n".join(lines)


def _make_discover_tool(catalog: ToolCatalog) -> AgentTool:
    """Build the ``discover`` meta-tool bound to *catalog*."""

    def handler(args: dict[str, Any], context: Any | None = None) -> str:
        return catalog.discover(
            query=str(args.get("query") or ""),
            category=args.get("category"),
            limit=args.get("limit") or _DEFAULT_LIMIT,
        )

    return AgentTool(
        name=DISCOVER_TOOL_NAME,
        description=(
            "Search and reveal tools that are currently masked to save "
            "context. Call with query='' to list the masked tool "
            "categories, or with a keyword/tool name to reveal the "
            "matching tools (they become callable from the next step). "
            "Use this before concluding a capability does not exist."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keyword(s) or tool name to search for. An empty "
                        "string lists the masked categories."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Restrict the search to a category (e.g. 'mcp_dokploy')."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of tools to reveal (default 10, max 25)."
                    ),
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )
