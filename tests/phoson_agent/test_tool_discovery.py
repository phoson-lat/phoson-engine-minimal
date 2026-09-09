"""Tests for cache-aware tool discovery (issue #148).

Two layers:

* ``ToolCatalog`` unit behaviour — activation rule, stable prefix,
  monotonic reveal, search scoring, overview, limits.
* Engine E2E with a fake chat — discover tool presence, masked tools
  absent from the ``tools`` payload, a reveal appearing from the *next*
  LLM call, and the KV-cache invariant: every payload is a prefix
  extension of the previous one.
"""

import json

import pytest

from phoson_agent import Plugin
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_agent.models import AgentTool, AgentStartEvent
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.tool_discovery import DISCOVER_TOOL_NAME, ToolCatalog
from phoson_agent.plugins.summarizer import TokenEstimator


def _tool(name: str, description: str = "A test tool.") -> AgentTool:
    return AgentTool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string", "description": name}},
            "required": ["q"],
        },
        handler=lambda args, context=None: f"{name} ok",
    )


def _catalog_weight(tools: list[AgentTool]) -> int:
    est = TokenEstimator()
    tds = [
        ToolDefinition(name=t.name, description=t.description, parameters=t.parameters)
        for t in tools
    ]
    return est.count_tools(tds)


class TestActivation:
    def test_inactive_without_budget(self):
        core = [_tool("bash"), _tool("read_file")]
        hidden = [_tool("mcp_alpha_do_thing")]
        cat = ToolCatalog(core, hidden, None)
        assert not cat.active
        assert [d.name for d in cat.definitions()] == [
            "bash",
            "read_file",
            "mcp_alpha_do_thing",
        ]
        assert cat.discover_tool is None

    def test_inactive_when_catalog_fits_budget(self):
        core = [_tool("bash")]
        hidden = [_tool("mcp_alpha_do_thing")]
        cat = ToolCatalog(core, hidden, _catalog_weight(core + hidden) + 1000)
        assert not cat.active

    def test_active_when_catalog_exceeds_budget(self):
        core = [_tool("bash")]
        hidden = [_tool("mcp_alpha_do_thing")]
        cat = ToolCatalog(core, hidden, _catalog_weight(core + hidden) // 2)
        assert cat.active
        assert cat.discover_tool is not None


class TestPrefixStability:
    def _catalog(self) -> ToolCatalog:
        core = [_tool("bash", "Run a shell command."), _tool("read_file")]
        hidden = [
            _tool("mcp_alpha_search_web", "Search the web for information."),
            _tool("mcp_alpha_fetch_page", "Fetch a web page."),
            _tool("mcp_beta_create_note", "Create a note in memory."),
        ]
        return ToolCatalog(core, hidden, _catalog_weight(core + hidden) // 2)

    def test_definitions_start_with_discover_then_core(self):
        cat = self._catalog()
        assert [d.name for d in cat.definitions()] == [
            DISCOVER_TOOL_NAME,
            "bash",
            "read_file",
        ]

    def test_reveal_appends_without_reordering(self):
        cat = self._catalog()
        before = [d.name for d in cat.definitions()]
        cat.discover(query="search")
        after = [d.name for d in cat.definitions()]
        assert after[: len(before)] == before  # KV-cache: prefix stable
        assert after[len(before) :] == ["mcp_alpha_search_web"]
        assert cat.hidden_count() == 2

    def test_reveal_is_monotonic(self):
        cat = self._catalog()
        cat.discover(query="search")
        assert cat.reveal(["mcp_alpha_search_web"]) == []
        cat.discover(query="note")
        names = [d.name for d in cat.definitions()]
        assert names.count("mcp_alpha_search_web") == 1


class TestDiscovery:
    def _catalog(self) -> ToolCatalog:
        core = [_tool("bash")]
        hidden = [
            _tool(
                "mcp_github_create_issue",
                "Create a new issue in a repository.",
            ),
            _tool("mcp_github_list_issues", "List issues in a repository."),
            _tool(
                "mcp_memory_create_entities",
                "Create entities in the knowledge graph.",
            ),
            _tool(
                "mcp_shell_run_command",
                "Run a command in a persistent shell.",
            ),
        ]
        return ToolCatalog(core, hidden, _catalog_weight(core + hidden) // 2)

    def test_overview_lists_categories(self):
        out = self._catalog().discover(query="")
        assert "mcp_github: 2 tool(s)" in out
        assert "mcp_memory: 1 tool(s)" in out
        assert "mcp_shell: 1 tool(s)" in out

    def test_name_match_ranks_first_and_reveals(self):
        cat = self._catalog()
        out = cat.discover(query="create issue")
        revealed = json.loads(out[out.index("[") :])
        assert revealed[0]["function"]["name"] == "mcp_github_create_issue"
        names = [d.name for d in cat.definitions()]
        assert "mcp_github_list_issues" in names

    def test_category_filter(self):
        out = self._catalog().discover(query="issue", category="mcp_memory")
        assert "No masked tools match" in out

    def test_limit_caps_reveal(self):
        cat = self._catalog()
        out = cat.discover(query="issue", limit=1)
        revealed = json.loads(out[out.index("[") :])
        assert len(revealed) == 1
        assert cat.hidden_count() == 3

    def test_no_match(self):
        assert "No masked tools match" in self._catalog().discover(query="zzzqqq")

    def test_inactive_catalog_refuses(self):
        cat = ToolCatalog([_tool("bash")], [_tool("mcp_x")], None)
        assert "not active" in cat.discover(query="x")


class _CaptureChat(BaseLLMChat):
    """Final answer after an optional ``discover`` call; records payloads."""

    def __init__(self) -> None:
        self.payloads: list[list[str]] = []
        self.call_discover = False
        self._iteration = 0

    async def stream(self, messages, config, tools=None):
        self._iteration += 1
        self.payloads.append([t.name for t in (tools or [])])
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        if self.call_discover and self._iteration == 1:
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_d",
                tool_name=DISCOVER_TOOL_NAME,
                args={"query": "search"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=10, output=5),
            cost_usd=0.0,
            cost_known=True,
        )
        yield LLMDoneEvent(content="ok", has_tool_calls=False)


class _StubPlugin(Plugin):
    def __init__(self, tools: list[AgentTool]) -> None:
        self._tools = tools

    @property
    def name(self) -> str:
        return "stub"

    @property
    def version(self) -> str:
        return "0.0.0"

    @property
    def description(self) -> str:
        return "test stub"

    def configure(self, config) -> None:
        return None

    def initialize(self) -> None:
        return None

    def cleanup(self) -> None:
        return None

    def get_tools(self) -> list[AgentTool]:
        return list(self._tools)

    def get_middlewares(self) -> list:
        return []

    def get_commands(self) -> list:
        return []


class TestEngineE2E:
    @pytest.mark.asyncio
    async def test_inactive_keeps_full_catalog(self):
        chat = _CaptureChat()
        engine = AgentEngine(
            chat=chat,
            tools=[_tool("bash"), _tool("read_file")],
            plugins=[
                _StubPlugin([_tool("mcp_alpha_search_web", "Search the web now.")])
            ],
        )
        events = [
            e
            async for e in engine.stream(
                [Message(role="user", content="hi")], ModelConfig(model="m")
            )
        ]
        start = next(e for e in events if isinstance(e, AgentStartEvent))
        assert start.tool_count == 3
        assert start.tool_masked == 0
        assert DISCOVER_TOOL_NAME not in chat.payloads[0]

    @pytest.mark.asyncio
    async def test_active_masks_and_reveals_prefix_stable(self):
        core = [_tool("bash", "Run a shell command."), _tool("read_file")]
        hidden = [
            _tool("mcp_alpha_search_web", "Search the web for information."),
            _tool("mcp_alpha_fetch_page", "Fetch a web page."),
        ]
        chat = _CaptureChat()
        chat.call_discover = True
        engine = AgentEngine(
            chat=chat,
            tools=core,
            plugins=[_StubPlugin(hidden)],
            tool_budget_tokens=_catalog_weight(core + hidden) // 2,
        )
        assert DISCOVER_TOOL_NAME in {t.name for t in engine.tools}
        events = [
            e
            async for e in engine.stream(
                [Message(role="user", content="hi")], ModelConfig(model="m")
            )
        ]
        start = next(e for e in events if isinstance(e, AgentStartEvent))
        assert start.tool_count == 3  # discover + 2 core
        assert start.tool_masked == 2
        first, second = chat.payloads
        assert first == [DISCOVER_TOOL_NAME, "bash", "read_file"]
        assert "mcp_alpha_search_web" in second
        assert "mcp_alpha_fetch_page" not in second
        assert second[: len(first)] == first  # KV-cache invariant
