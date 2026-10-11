"""Explicit prompt-caching tests for the Alibaba (Model Studio) adapter.

Model Studio's explicit cache is enabled per request by placing
``"cache_control": {"type": "ephemeral"}`` on a message content part, which
caches everything from the start of the ``messages`` array up to that part.
The adapter must tag the stable prefix (system prompt — tool definitions ride
along with it for caching) and the last message, so the cached prefix advances
with the conversation. See the Model Studio "Explicit Cache Best Practices"
guide.

The Anthropic adapter has the equivalent coverage in
``test_anthropic_caching_unit.py``; this file covers the OpenAI-compatible
code path.
"""

from dataclasses import field, dataclass
from collections.abc import AsyncIterator

import pytest

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    ImageBlock,
    UsageEvent,
    ModelConfig,
    ToolUseBlock,
    ToolDefinition,
    ToolResultBlock,
)
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.alibaba import AlibabaChat, supports_explicit_cache
from phoson_llm.chats._openai_compatible import (
    _EPHEMERAL_CACHE_CONTROL,
    _build_request_kwargs,
    stream_chat_completions,
)

# ─── Fake OpenAI SDK objects ─────────────────────────────────────────────────


@dataclass
class _PromptDetails:
    cached_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _PromptDetails = field(default_factory=_PromptDetails)


@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Chunk:
    choices: list[_Choice] = field(default_factory=list)
    usage: _Usage | None = None


class _FakeStream:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> AsyncIterator[_Chunk]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_Chunk]:
        for chunk in self._chunks:
            yield chunk


class _FakeCompletions:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs: object) -> _FakeStream:
        self.last_kwargs = dict(kwargs)
        return _FakeStream(self._chunks)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self.chat = _FakeChat(_FakeCompletions(chunks))


def _alibaba(chunks: list[_Chunk]) -> tuple[AlibabaChat, _FakeCompletions]:
    chat = AlibabaChat(api_key="test-key")
    client = _FakeClient(chunks)
    chat._client = client  # type: ignore[assignment]
    return chat, client.chat.completions


async def _collect(it: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [ev async for ev in it]


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


def _build(messages, *, cache, system=None, tools=None):
    return _build_request_kwargs(
        config=ModelConfig(model="qwen3.7-max", system=system),
        messages=messages,
        tools=tools,
        max_tokens_key="max_tokens",
        cache=cache,
    )


# ─── Marker shape ────────────────────────────────────────────────────────────


def test_ephemeral_marker_shape() -> None:
    # 5-minute TTL, renewed on each hit — the plain ephemeral marker.
    assert _EPHEMERAL_CACHE_CONTROL == {"type": "ephemeral"}


# ─── System + last-message anchors ───────────────────────────────────────────


def test_cache_tags_system_and_last_message_only() -> None:
    kwargs = _build(
        [
            Message(role="user", content="one"),
            Message(role="assistant", content="two"),
            Message(role="user", content="three"),
        ],
        cache=True,
        system="You are Phos.",
    )
    messages = kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == [
        {
            "type": "text",
            "text": "You are Phos.",
            "cache_control": _EPHEMERAL_CACHE_CONTROL,
        }
    ]
    # The middle turns stay untagged; only the last message advances the cache.
    assert _collect_cache_control(messages[1:3]) == []
    assert messages[-1]["content"] == [
        {"type": "text", "text": "three", "cache_control": _EPHEMERAL_CACHE_CONTROL}
    ]
    # Exactly two markers: system prefix + last message (max four allowed).
    assert _collect_cache_control(kwargs) == [_EPHEMERAL_CACHE_CONTROL] * 2


def test_system_from_message_list_is_tagged() -> None:
    kwargs = _build(
        [
            Message(role="system", content="sys prompt"),
            Message(role="user", content="hi"),
        ],
        cache=True,
    )
    assert kwargs["messages"][0]["content"] == [
        {
            "type": "text",
            "text": "sys prompt",
            "cache_control": _EPHEMERAL_CACHE_CONTROL,
        }
    ]


def test_config_system_replaces_and_tags_message_system() -> None:
    kwargs = _build(
        [
            Message(role="system", content="stale"),
            Message(role="user", content="hi"),
        ],
        cache=True,
        system="fresh",
    )
    assert kwargs["messages"][0]["content"] == [
        {"type": "text", "text": "fresh", "cache_control": _EPHEMERAL_CACHE_CONTROL}
    ]
    assert [m["role"] for m in kwargs["messages"]].count("system") == 1


def test_no_markers_without_cache_flag() -> None:
    kwargs = _build(
        [Message(role="user", content="a"), Message(role="assistant", content="b")],
        cache=False,
        system="You are Phos.",
    )
    assert _collect_cache_control(kwargs) == []
    # Content stays in string form when caching is off.
    assert kwargs["messages"][0]["content"] == "You are Phos."


def test_single_user_message_gets_one_marker() -> None:
    kwargs = _build([Message(role="user", content="hi")], cache=True)
    assert _collect_cache_control(kwargs) == [_EPHEMERAL_CACHE_CONTROL]


def test_tools_are_not_marked_but_covered_by_system_marker() -> None:
    # Model Studio treats the tool definitions as part of the system prompt for
    # caching, so no marker is placed on the tools array itself (which also
    # avoids a third breakpoint); the system marker covers them.
    kwargs = _build(
        [Message(role="user", content="hi")],
        cache=True,
        system="You are Phos.",
        tools=[_tool("bash"), _tool("read_file")],
    )
    assert _collect_cache_control(kwargs["tools"]) == []
    assert _collect_cache_control(kwargs["messages"][0]) == [_EPHEMERAL_CACHE_CONTROL]


# ─── Last-message anchor edge cases ──────────────────────────────────────────


def test_last_message_anchor_marks_last_text_block() -> None:
    kwargs = _build(
        [
            Message(role="user", content="earlier"),
            Message(
                role="user",
                content=[
                    TextBlock(text="look at this"),
                    ImageBlock(source="https://example.com/x.png"),
                ],
            ),
        ],
        cache=True,
    )
    blocks = kwargs["messages"][-1]["content"]
    # The text part carries the marker; the image part does not.
    assert blocks[0]["cache_control"] == _EPHEMERAL_CACHE_CONTROL
    assert "cache_control" not in blocks[1]


def test_tool_result_message_is_anchored() -> None:
    # In a ReAct loop the last message is usually a tool-result turn with
    # string content; the anchor expands it to array form so the marker still
    # applies to the newest fragment.
    kwargs = _build(
        [
            Message(role="user", content="earlier"),
            Message(
                role="user",
                content=[ToolResultBlock("id1", "output", error=False)],
            ),
        ],
        cache=True,
    )
    tool_msg = kwargs["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == [
        {"type": "text", "text": "output", "cache_control": _EPHEMERAL_CACHE_CONTROL}
    ]


def test_assistant_tool_call_only_last_message_stays_untagged() -> None:
    # An assistant turn that only emitted tool_calls has "" content: nothing to
    # anchor on, so the marker is skipped rather than producing an empty part.
    kwargs = _build(
        [
            Message(role="user", content="earlier"),
            Message(role="assistant", content=[ToolUseBlock("id1", "bash", {})]),
        ],
        cache=True,
    )
    assert _collect_cache_control(kwargs["messages"][-1]) == []


# ─── Parallel tool results (20-block lookback window) ────────────────────────


def _two_tool_turn() -> list[Message]:
    return [
        Message(role="user", content="do two things"),
        Message(
            role="assistant",
            content=[
                ToolUseBlock("id1", "bash", {}),
                ToolUseBlock("id2", "read_file", {}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock("id1", "out1", error=False),
                ToolResultBlock("id2", "out2", error=False),
            ],
        ),
    ]


def test_parallel_tool_results_are_coalesced_into_one_message() -> None:
    """A parallel tool-call turn emits one ``tool`` message per result; the
    explicit-cache path fuses them into a single multi-block message so the
    last-message marker stays inside Model Studio's 20-block lookback window."""
    kwargs = _build(_two_tool_turn(), cache=True, system="You are Phos.")

    tool_msgs = [m for m in kwargs["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    merged = tool_msgs[0]
    # First block rides on the message's own tool_call_id; the rest carry theirs.
    assert merged["tool_call_id"] == "id1"
    assert merged["content"][0] == {"type": "text", "text": "out1"}
    assert merged["content"][1]["text"] == "out2"
    assert merged["content"][1]["tool_call_id"] == "id2"
    # The merged message is the last one, so its final block carries the marker.
    assert merged["content"][-1]["cache_control"] == _EPHEMERAL_CACHE_CONTROL


def test_parallel_tool_results_stay_separate_without_cache() -> None:
    """Plain OpenAI forbids one message per tool_call_id inside a content
    block, so the fusion must not happen when explicit cache is off."""
    kwargs = _build(_two_tool_turn(), cache=False, system="You are Phos.")

    tool_msgs = [m for m in kwargs["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert all(isinstance(m["content"], str) for m in tool_msgs)


def test_single_tool_result_is_not_coalesced() -> None:
    """A run of length one is left exactly as-is (string content)."""
    kwargs = _build(
        [
            Message(role="user", content="do a thing"),
            Message(role="assistant", content=[ToolUseBlock("id1", "bash", {})]),
            Message(
                role="user",
                content=[ToolResultBlock("id1", "out", error=False)],
            ),
        ],
        cache=True,
        system="You are Phos.",
    )
    tool_msgs = [m for m in kwargs["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == [
        {"type": "text", "text": "out", "cache_control": _EPHEMERAL_CACHE_CONTROL}
    ]


def test_non_adjacent_tool_messages_are_not_fused() -> None:
    """Only *consecutive* tool messages merge; a turn in between separates them."""
    kwargs = _build(
        [
            Message(role="assistant", content=[ToolUseBlock("id1", "bash", {})]),
            Message(role="user", content=[ToolResultBlock("id1", "out1", False)]),
            Message(role="user", content="next"),
            Message(role="assistant", content=[ToolUseBlock("id2", "bash", {})]),
            Message(role="user", content=[ToolResultBlock("id2", "out2", False)]),
        ],
        cache=True,
    )
    tool_msgs = [m for m in kwargs["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2


# ─── Adapter wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alibaba_stream_sends_cache_markers() -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"), finish_reason="stop")]),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=1)),
    ]
    chat, completions = _alibaba(chunks)

    await _collect(
        chat.stream(
            [
                Message(role="user", content="one"),
                Message(role="assistant", content="two"),
                Message(role="user", content="three"),
            ],
            ModelConfig(model="qwen3.7-max", system="You are Phos."),
        )
    )

    assert completions.last_kwargs is not None
    assert (
        _collect_cache_control(completions.last_kwargs["messages"])
        == [_EPHEMERAL_CACHE_CONTROL] * 2
    )


@pytest.mark.asyncio
async def test_openai_adapter_defaults_to_no_cache_markers() -> None:
    # The opt-in flag defaults off, so other OpenAI-compatible backends are
    # unaffected by the Alibaba caching path.
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"), finish_reason="stop")]),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=1)),
    ]
    chat = OpenAIChat(api_key="test-key")
    client = _FakeClient(chunks)
    chat._client = client  # type: ignore[assignment]

    await _collect(
        chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="gpt-4o-mini", system="You are Phos."),
        )
    )

    assert client.chat.completions.last_kwargs is not None
    assert _collect_cache_control(client.chat.completions.last_kwargs) == []


# ─── Usage reporting ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_reports_alibaba_cache_tokens() -> None:
    """``cache_creation_input_tokens`` (write) and ``cached_tokens`` (read)
    must surface on the UsageEvent — the metrics the CLI accumulates."""
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"), finish_reason="stop")]),
        _Chunk(
            usage=_Usage(
                prompt_tokens=2100,
                completion_tokens=5,
                prompt_tokens_details=_PromptDetails(
                    cached_tokens=0, cache_creation_input_tokens=2005
                ),
            )
        ),
    ]
    chat, _ = _alibaba(chunks)

    events = await _collect(
        chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="alibaba/qwen3.7-max", system="You are Phos."),
        )
    )
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert usage.usage.cache_write == 2005
    assert usage.usage.cache_read == 0
    # Gross prompt_tokens stay on the event for display...
    assert usage.usage.input == 2100
    # ...but the cost must price the *net* input (2100 - 2005 = 95 standard
    # tokens) plus the cache-write tokens — never the gross count, which would
    # bill the cached tokens twice.
    # qwen3.7-max: input 2.50, output 7.50, cache_write 3.125 per Mtok.
    expected = (95 * 2.50 + 5 * 7.50 + 2005 * 3.125) / 1_000_000
    assert usage.cost_known is True
    assert usage.cost_usd == pytest.approx(expected, abs=1e-8)


@pytest.mark.asyncio
async def test_usage_reports_cache_hit() -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"), finish_reason="stop")]),
        _Chunk(
            usage=_Usage(
                prompt_tokens=2100,
                completion_tokens=5,
                prompt_tokens_details=_PromptDetails(
                    cached_tokens=2005, cache_creation_input_tokens=0
                ),
            )
        ),
    ]
    chat, _ = _alibaba(chunks)

    events = await _collect(
        chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="alibaba/qwen3.7-max", system="You are Phos."),
        )
    )
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert usage.usage.cache_read == 2005
    assert usage.usage.cache_write == 0
    # Net input = 2100 - 2005 = 95 standard tokens + 2005 cache-read tokens.
    # qwen3.7-max: input 2.50, output 7.50, cache_read 0.25 per Mtok.
    expected = (95 * 2.50 + 5 * 7.50 + 2005 * 0.25) / 1_000_000
    assert usage.cost_usd == pytest.approx(expected, abs=1e-8)


# ─── Model gating ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "qwen3.8-max",
        "alibaba/qwen3.8-max",
        "qwen3.8-max-0902",
        "qwen3.7-max-2026-05-20",
        "qwen-plus",
        "qwen-plus-latest",
        "qwen-flash",
        "qwen3-coder-flash",
        "deepseek-v3.2",
        "kimi-k2.6",
        "glm-5.1",
        "QWEN3.8-FLASH",
    ],
)
def test_supports_explicit_cache_accepts_documented_models(model: str) -> None:
    assert supports_explicit_cache(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "qwen-turbo",
        "qwq-plus",
        "qwen-max",  # implicit-cache only
        "qwen-plus-character",  # must not be captured by the qwen-plus prefix
        "gpt-4o-mini",
        "unknown-model",
    ],
)
def test_supports_explicit_cache_rejects_others(model: str) -> None:
    assert supports_explicit_cache(model) is False


@pytest.mark.asyncio
async def test_alibaba_skips_markers_for_unsupported_model() -> None:
    # qwen-turbo has no explicit cache: the adapter must not send markers, so
    # the request stays on Model Studio's implicit-cache path.
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"), finish_reason="stop")]),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=1)),
    ]
    chat, completions = _alibaba(chunks)

    await _collect(
        chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="qwen-turbo", system="You are Phos."),
        )
    )

    assert completions.last_kwargs is not None
    assert _collect_cache_control(completions.last_kwargs) == []


@pytest.mark.asyncio
async def test_stream_direct_cache_flag_is_threaded() -> None:
    chunks = [_Chunk(usage=_Usage(prompt_tokens=1, completion_tokens=1))]
    client = _FakeClient(chunks)

    await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[
                Message(role="user", content="one"),
                Message(role="user", content="two"),
            ],
            config=ModelConfig(model="qwen3.7-max", system="sys"),
            cache=True,
        )
    )
    kwargs = client.chat.completions.last_kwargs
    assert kwargs is not None
    assert _collect_cache_control(kwargs["messages"]) == [_EPHEMERAL_CACHE_CONTROL] * 2
