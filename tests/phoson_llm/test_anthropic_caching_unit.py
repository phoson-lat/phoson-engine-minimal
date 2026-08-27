"""Prompt-caching tests for the Anthropic adapter (IMPROVEMENTS.md G2 / #69).

The adapter must send ephemeral ``cache_control`` breakpoints on the
stable prefix (system prompt, tool list) and on the last message of the
conversation so the cached prefix advances as the history grows.
"""

from dataclasses import dataclass

from phoson_llm.schemas import (
    Message,
    TextBlock,
    UsageEvent,
    ModelConfig,
    ToolUseBlock,
    ToolDefinition,
    ToolResultBlock,
)
from phoson_llm.chats.anthropic import (
    _EPHEMERAL,
    AnthropicChat,
    _convert_tools,
    _convert_messages,
)

# ─── Fake anthropic SDK objects ──────────────────────────────────────────────


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_creation_input_tokens: int = 3
    cache_read_input_tokens: int = 7


@dataclass
class _FakeFinalMessage:
    usage: _FakeUsage = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = _FakeUsage()


class _FakeEvents:
    """Async-iterable of stream events (empty) + final message."""

    def __init__(self, captured: dict) -> None:
        self.captured = captured

    def __aiter__(self):
        return self._events().__aiter__()

    async def _events(self):
        return
        yield  # pragma: no cover — makes this an async generator

    async def get_final_message(self):
        return _FakeFinalMessage()


class _FakeStreamContext:
    """Mimics ``client.messages.stream(**kwargs)``: an async context
    manager that captures the kwargs and yields the event source."""

    def __init__(self, captured: dict) -> None:
        self.captured = captured

    def __call__(self, **kwargs):
        self.captured.clear()
        self.captured.update(kwargs)
        return self

    async def __aenter__(self):
        return _FakeEvents(self.captured)

    async def __aexit__(self, *exc):
        return False


class _FakeMessages:
    def __init__(self, captured: dict) -> None:
        self.stream = _FakeStreamContext(captured)


class _FakeClient:
    def __init__(self, captured: dict) -> None:
        self.messages = _FakeMessages(captured)


def _chat(captured: dict) -> AnthropicChat:
    chat = AnthropicChat(api_key="test-key")
    chat._client = _FakeClient(captured)
    return chat


def _tool(name: str = "bash") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
    )


def _collect_cache_control(obj) -> list:
    """Recursively collect every ``cache_control`` value in a payload."""
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "cache_control":
                found.append(value)
            else:
                found.extend(_collect_cache_control(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect_cache_control(item))
    return found


# ─── System prompt breakpoint ────────────────────────────────────────────────


async def test_system_prompt_gets_ephemeral_breakpoint() -> None:
    captured: dict = {}
    chat = _chat(captured)

    events = [
        e
        async for e in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="claude-sonnet-4-6", system="You are Phos."),
        )
    ]

    system = captured["system"]
    assert isinstance(system, list), (
        "system must be a block list to carry cache_control"
    )
    assert system == [
        {"type": "text", "text": "You are Phos.", "cache_control": _EPHEMERAL}
    ]
    # The rest of the stream contract still holds.
    assert any(isinstance(e, UsageEvent) for e in events)


async def test_system_from_message_list_also_gets_breakpoint() -> None:
    """When config.system is absent the system message is extracted —
    it must still be sent as a cached block list."""
    captured: dict = {}
    chat = _chat(captured)

    async for _ in chat.stream(
        [
            Message(role="system", content="sys prompt"),
            Message(role="user", content="hi"),
        ],
        ModelConfig(model="claude-sonnet-4-6"),
    ):
        pass

    assert captured["system"] == [
        {"type": "text", "text": "sys prompt", "cache_control": _EPHEMERAL}
    ]


# ─── Tools breakpoint ────────────────────────────────────────────────────────


def test_tools_get_breakpoint_on_last_definition() -> None:
    out = _convert_tools([_tool("bash"), _tool("read_file")], cache_last=True)
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == _EPHEMERAL


def test_tools_without_cache_flag_are_untouched() -> None:
    out = _convert_tools([_tool("bash")], cache_last=False)
    assert _collect_cache_control(out) == []


def test_single_tool_gets_the_breakpoint() -> None:
    out = _convert_tools([_tool("bash")], cache_last=True)
    assert out[0]["cache_control"] == _EPHEMERAL


# ─── Last-message anchor ─────────────────────────────────────────────────────


def test_string_last_message_anchored_as_block_list() -> None:
    out = _convert_messages(
        [Message(role="user", content="first"), Message(role="user", content="last")],
        cache_last=True,
    )
    assert out[0] == {"role": "user", "content": "first"}
    assert out[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "last", "cache_control": _EPHEMERAL},
        ],
    }


def test_anchor_skips_earlier_messages() -> None:
    out = _convert_messages(
        [
            Message(role="user", content="one"),
            Message(role="assistant", content="two"),
            Message(role="user", content="three"),
        ],
        cache_last=True,
    )
    assert _collect_cache_control(out[:-1]) == []
    assert _collect_cache_control(out[-1]) == [_EPHEMERAL]


def test_anchor_lands_on_last_block_not_tool_use() -> None:
    """tool_use blocks reject cache_control — the anchor must fall back
    to the last cacheable block of the message."""
    out = _convert_messages(
        [
            Message(
                role="assistant",
                content=[TextBlock(text="calling"), ToolUseBlock("id1", "bash", {})],
            )
        ],
        cache_last=True,
    )
    blocks = out[-1]["content"]
    assert blocks[0]["cache_control"] == _EPHEMERAL
    assert "cache_control" not in blocks[1]


def test_tool_result_can_be_anchored() -> None:
    """In a ReAct loop the last message is usually a user turn with tool
    results — the API accepts cache_control there, and it is exactly the
    fragment that must stay cached for the next turn."""
    out = _convert_messages(
        [
            Message(
                role="user",
                content=[
                    ToolResultBlock("id1", "output", error=False),
                    TextBlock(text="continuing"),
                ],
            )
        ],
        cache_last=True,
    )
    blocks = out[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == _EPHEMERAL


def test_anchor_on_last_block_when_result_is_last() -> None:
    out = _convert_messages(
        [
            Message(
                role="user",
                content=[TextBlock(text="text"), ToolResultBlock("id1", "output")],
            )
        ],
        cache_last=True,
    )
    blocks = out[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == _EPHEMERAL


def test_no_anchor_when_last_message_is_tool_use_only() -> None:
    out = _convert_messages(
        [
            Message(role="user", content="earlier"),
            Message(
                role="assistant",
                content=[ToolUseBlock("id1", "bash", {})],
            ),
        ],
        cache_last=True,
    )
    assert _collect_cache_control(out) == []


def test_no_breakpoints_without_cache_flag() -> None:
    out = _convert_messages(
        [
            Message(role="user", content="a"),
            Message(role="assistant", content="b"),
        ],
        cache_last=False,
    )
    assert _collect_cache_control(out) == []


def test_system_messages_are_dropped_from_conversation() -> None:
    """System messages go through the top-level system param, not the
    messages array (existing contract, kept with caching on)."""
    out = _convert_messages(
        [
            Message(role="system", content="sys"),
            Message(role="user", content="hi"),
        ],
        cache_last=True,
    )
    assert all(m["role"] != "system" for m in out)
    assert _collect_cache_control(out) == [_EPHEMERAL]


# ─── Full request: exactly the three reserved breakpoints ───────────────────


async def test_full_request_uses_three_breakpoints() -> None:
    captured: dict = {}
    chat = _chat(captured)

    async for _ in chat.stream(
        [
            Message(role="user", content="one"),
            Message(role="assistant", content="two"),
            Message(role="user", content="three"),
        ],
        ModelConfig(model="claude-sonnet-4-6", system="You are Phos."),
        tools=[_tool("bash"), _tool("read_file")],
    ):
        pass

    assert _collect_cache_control(captured["system"]) == [_EPHEMERAL]
    assert _collect_cache_control(captured["tools"]) == [_EPHEMERAL]
    assert _collect_cache_control(captured["messages"]) == [_EPHEMERAL]
    assert len(_collect_cache_control(captured)) == 3, (
        "at most four breakpoints are allowed; the adapter reserves three"
    )


async def test_usage_event_exposes_cache_tokens() -> None:
    """cache_creation/cache_read usage must surface on the UsageEvent —
    the metrics the CLI accumulates in SessionMetrics."""
    captured: dict = {}
    chat = _chat(captured)

    events = [
        e
        async for e in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="anthropic/claude-sonnet-4-6", system="You are Phos."),
        )
    ]

    usage_event = next(e for e in events if isinstance(e, UsageEvent))
    assert usage_event.usage.input == 10
    assert usage_event.usage.output == 5
    assert usage_event.usage.cache_write == 3
    assert usage_event.usage.cache_read == 7
    # Known model with cache tokens → cost is known and reflects them.
    assert usage_event.cost_known is True
    assert usage_event.cost_usd > 0


def test_ephemeral_marker_shape() -> None:
    """The marker sent to the API is the plain ephemeral TTL (5 min
    default; the 1-hour TTL would double the cache-write price and buys
    nothing for turn-by-turn agentic traffic)."""
    assert _EPHEMERAL == {"type": "ephemeral"}
