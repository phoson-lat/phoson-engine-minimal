"""Unit tests for the Mistral adapter (mistralai SDK, mocked)."""

import pytest

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ToolCallDeltaEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.mistral import (
    MistralChat,
    _convert_tools_mistral,
    _convert_messages_mistral,
)

mistralai = pytest.importorskip("mistralai")

from mistralai.client.models.toolcall import ToolCall, FunctionCall  # noqa: E402
from mistralai.client.models.deltamessage import DeltaMessage  # noqa: E402
from mistralai.client.models.completionchunk import CompletionChunk  # noqa: E402
from mistralai.client.models.completionevent import CompletionEvent  # noqa: E402
from mistralai.client.models.completionresponsestreamchoice import (  # noqa: E402
    CompletionResponseStreamChoice,
)

# ─── Basic ───────────────────────────────────────────────────────────────────


def test_is_base_llm_chat_subclass():
    chat = MistralChat(api_key="test-key")
    assert isinstance(chat, BaseLLMChat)


def test_repr_includes_mistral():
    chat = MistralChat(api_key="test-key")
    assert "Mistral" in repr(chat)


# ─── Message / tool conversion ───────────────────────────────────────────────


def test_convert_tools_mistral_shape():
    tools = [
        ToolDefinition(name="bash", description="run", parameters={"type": "object"})
    ]
    out = _convert_tools_mistral(tools)
    assert out[0].type == "function"
    assert out[0].function.name == "bash"
    assert out[0].function.description == "run"
    assert out[0].function.parameters == {"type": "object"}


def test_convert_messages_handles_tool_use_and_result():
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(tool_call_id="t1", tool_name="bash", args={"cmd": "ls"})
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_call_id="t1", result="ok", error=False)],
        ),
    ]
    out = _convert_messages_mistral(msgs)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "hi"}
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"] == [
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"cmd": "ls"}'},
        }
    ]
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}


# ─── Streaming helpers ───────────────────────────────────────────────────────


def _chunk(delta, finish_reason=None):
    choice = CompletionResponseStreamChoice(
        index=0, delta=delta, finish_reason=finish_reason
    )
    ck = CompletionChunk(id="1", model="m", choices=[choice], object="chunk", created=0)
    return CompletionEvent(data=ck)


class _AsyncIter:
    """Minimal async iterable over a fixed list of chunks."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _streaming_client(chunks):
    """Build a fake client whose ``chat.stream_async`` returns *chunks*.

    The real SDK's ``stream_async`` is a *coroutine* that returns an async
    iterable (``EventStreamAsync``), not an async generator — the fake must
    mirror that shape or the adapter's ``await`` fails.
    """

    class FakeChat:
        def __init__(self, chunks):
            self._chunks = chunks
            self.calls = []

        async def stream_async(self, **kwargs):
            self.calls.append(kwargs)
            return _AsyncIter(self._chunks)

    class FakeClient:
        def __init__(self, chunks):
            self.chat = FakeChat(chunks)

    return FakeClient(chunks)


# ─── Streaming: text + tools ─────────────────────────────────────────────────


async def test_mistral_stream_emits_tokens_and_tool_calls():
    chunks = [
        _chunk(DeltaMessage(role="assistant", content="Let me ")),
        _chunk(DeltaMessage(role="assistant", content="check.")),
        _chunk(
            DeltaMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        index=0,
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="get_weather", arguments='{"ci'),
                    )
                ],
            )
        ),
        # Continuation delta: no id (the SDK's FunctionCall requires a
        # string name, so the name is repeated — the accumulator ignores
        # repeats).
        _chunk(
            DeltaMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        index=0,
                        id=None,
                        type="function",
                        function=FunctionCall(
                            name="get_weather", arguments='ty":"Paris"}'
                        ),
                    )
                ],
            )
        ),
        _chunk(
            DeltaMessage(role="assistant", content=None),
            finish_reason="tool_calls",
        ),
    ]

    chat = MistralChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="mistral-large", temperature=0.4, max_tokens=80)
    tools = [
        ToolDefinition(
            name="get_weather", description="w", parameters={"type": "object"}
        )
    ]

    events = [
        e async for e in chat.stream([Message(role="user", content="hi")], cfg, tools)
    ]

    types = [type(e) for e in events]
    assert types[0] is LLMStartEvent
    assert types[-1] is LLMDoneEvent
    assert TokenEvent in types
    assert ToolCallDeltaEvent in types
    assert ToolCallEvent in types

    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert "".join(t.content for t in tokens) == "Let me check."

    tool = next(e for e in events if isinstance(e, ToolCallEvent))
    assert tool.tool_name == "get_weather"
    assert tool.tool_call_id == "call_1"
    assert tool.args == {"city": "Paris"}

    done = events[-1]
    assert done.has_tool_calls is True


async def test_mistral_stream_passes_tools_param():
    chunks = [
        _chunk(DeltaMessage(role="assistant", content="ok"), finish_reason="stop"),
    ]
    chat = MistralChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)
    tools = [
        ToolDefinition(name="bash", description="run", parameters={"type": "object"})
    ]

    async for _ in chat.stream([Message(role="user", content="hi")], cfg, tools):
        pass

    call = chat._client.chat.calls[0]
    assert call["model"] == "m"
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 10
    assert call["tools"] is not None
    assert call["tools"][0].function.name == "bash"


async def test_mistral_stream_no_tools_param_when_none():
    chunks = [
        _chunk(DeltaMessage(role="assistant", content="ok"), finish_reason="stop"),
    ]
    chat = MistralChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    async for _ in chat.stream([Message(role="user", content="hi")], cfg):
        pass

    call = chat._client.chat.calls[0]
    assert call["tools"] is None


async def test_mistral_stream_usage_event():
    from mistralai.client.models.usageinfo import UsageInfo

    choice = CompletionResponseStreamChoice(
        index=0,
        delta=DeltaMessage(role="assistant", content="hi"),
        finish_reason="stop",
    )
    ck = CompletionChunk(
        id="1",
        model="m",
        choices=[choice],
        object="chunk",
        created=0,
        usage=UsageInfo(prompt_tokens=7, completion_tokens=3),
    )
    chunks = [CompletionEvent(data=ck)]

    chat = MistralChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].usage.input == 7
    assert usage_events[0].usage.output == 3


# ─── Error handling ──────────────────────────────────────────────────────────


async def test_mistral_emits_error_event_on_exception():
    class FakeChat:
        async def stream_async(self, **kwargs):
            raise RuntimeError("boom")

    class FakeClient:
        chat = FakeChat()

    chat = MistralChat(api_key="test-key")
    chat._client = FakeClient()
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    assert isinstance(events[-1], ErrorEvent)
    assert "boom" in events[-1].message
    assert events[-1].code == "provider_error"
