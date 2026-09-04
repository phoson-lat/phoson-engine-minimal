"""Tests for issue #184 (F-23, F-24).

F-23: the sub-agent receives a real system prompt (cwd, date, platform,
AGENTS.md, its own tool list) instead of none.
F-24: the sub-agent is never offered the ``agent``/``agents`` delegation
tools, so it cannot (accidentally) delegate further.
"""

from unittest.mock import patch

import pytest

from phoson_llm.schemas import ModelConfig, LLMDoneEvent
from phoson_llm.chats.base import BaseLLMChat
from phoson_cli.tools.subagent import agent, agents, _select_tools


class _RecordingChat(BaseLLMChat):
    """Captures the ModelConfig (system) and tool names the engine streams.

    Records into a shared ``_rec`` list: the engine streams to a
    ``copy.copy`` of this chat (see ``_clone_chat``), which shares mutable
    attributes, so the original still sees what the clone recorded.
    """

    def __init__(self):
        self._rec: dict = {"system": None, "tool_names": []}

    async def stream(self, messages, config: ModelConfig, tools=None):
        # The engine passes ToolDefinition objects (or dicts) — accept both.
        self._rec["system"] = config.system
        self._rec["tool_names"] = [
            (t.get("name") if isinstance(t, dict) else getattr(t, "name", None))
            for t in (tools or [])
        ]
        yield LLMDoneEvent(content="done", has_tool_calls=False)

    @property
    def system(self):
        return self._rec["system"]

    @property
    def tool_names(self):
        return self._rec["tool_names"]


class _ProbeTool:
    name = "read_file"
    description = "read a file"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    async def handler(self, args, context=None):
        return "ok"


def _ctx(chat, tools):
    return {
        "chat": chat,
        "available_tools": tools,
        "default_model": "m",
        "max_iterations": 2,
        "safe_mode": False,
        "subagent_timeout_seconds": 30.0,
    }


# ── F-23: the child gets a real system prompt ───────────────────────────────


@pytest.mark.asyncio
async def test_subagent_receives_system_prompt_with_cwd_and_agents_md(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    chat = _RecordingChat()
    with patch("phoson_cli.session_utils.build_system_prompt") as mock_build:
        mock_build.return_value = (
            f"You are Phos in {tmp_path}. "
            "Distinctive-memory: distinctive-subagent-memory-99"
        )
        await agent.handler({"task": "do it"}, _ctx(chat, {"read_file": _ProbeTool()}))

    # The child's system prompt was built from ITS OWN tool subset ...
    mock_build.assert_called_once()
    # ... and was actually passed to the model via ModelConfig.system ...
    assert chat.system is not None
    assert "distinctive-subagent-memory-99" in chat.system
    assert str(tmp_path) in chat.system
    # ... plus the sub-agent framing line.
    assert "Sub-agent" in chat.system
    assert "Do not call the `agent`" in chat.system


@pytest.mark.asyncio
async def test_subagent_system_prompt_lists_its_own_tools(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    chat = _RecordingChat()
    await agent.handler({"task": "do it"}, _ctx(chat, {"read_file": _ProbeTool()}))
    # The tool list in the system prompt is derived from the child's subset.
    assert "read_file" in chat.system
    # Delegation tools must not be advertised to the child.
    assert "Available tools: read_file" in chat.system


# ── F-24: the child is never offered agent/agents ───────────────────────────


def test_select_tools_strips_agent_and_agents() -> None:
    selected, err = _select_tools(
        {"agent": object(), "agents": object(), "read_file": _ProbeTool()}, None
    )
    assert err is None
    assert "agent" not in selected
    assert "agents" not in selected
    assert "read_file" in selected


def test_select_tools_requested_agent_is_not_granted() -> None:
    # Asking for `agent` explicitly must not grant it (it's not available).
    selected, err = _select_tools(
        {"agent": object(), "read_file": _ProbeTool()}, ["agent", "read_file"]
    )
    assert err is not None  # "agent" is not in the allowed set
    assert "agent" not in selected


@pytest.mark.asyncio
async def test_subagent_engine_never_streams_delegation_tools(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    chat = _RecordingChat()
    await agent.handler(
        {"task": "do it"},
        _ctx(chat, {"agent": agent, "agents": agents, "read_file": _ProbeTool()}),
    )
    assert "agent" not in chat.tool_names
    assert "agents" not in chat.tool_names
    assert "read_file" in chat.tool_names
