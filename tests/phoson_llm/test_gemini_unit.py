"""Unit tests for the Gemini adapter (google-genai SDK, mocked)."""

import base64

import pytest

from phoson_llm.schemas import (
    Message,
    AudioBlock,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    DocumentBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.gemini import GeminiChat, _convert_messages

google = pytest.importorskip("google.genai")

from google.genai import types  # noqa: E402

# ─── Basic ───────────────────────────────────────────────────────────────────


def test_is_base_llm_chat_subclass():
    chat = GeminiChat(api_key="test-key")
    assert isinstance(chat, BaseLLMChat)


def test_default_api_key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    chat = GeminiChat()
    assert chat._api_key == "env-key"


def test_repr_includes_gemini():
    chat = GeminiChat(api_key="test-key")
    assert "Gemini" in repr(chat)


# ─── Message conversion: multimodal ──────────────────────────────────────────


def test_local_image_becomes_inline_base64(tmp_path):
    """Regression for #53: file:// images must be read and inlined, not
    passed as a local path to Part.from_uri (Gemini can't fetch those)."""
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNGfake")

    out = _convert_messages(
        [
            Message(
                role="user",
                content=[
                    ImageBlock(source=f"file://{img}", media_type="image/png"),
                ],
            )
        ]
    )
    part = out[0].parts[0]
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "image/png"
    assert base64.b64decode(part.inline_data.data) == b"\x89PNGfake"


def test_hosted_uri_passes_through_as_file_uri():
    out = _convert_messages(
        [
            Message(
                role="user",
                content=[
                    ImageBlock(source="gs://bucket/pic.png", media_type="image/png"),
                ],
            )
        ]
    )
    assert out[0].parts[0].file_data.file_uri == "gs://bucket/pic.png"


def test_local_pdf_becomes_inline_base64(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-fake")

    out = _convert_messages(
        [
            Message(
                role="user",
                content=[
                    DocumentBlock(source=f"file://{pdf}"),
                ],
            )
        ]
    )
    part = out[0].parts[0]
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "application/pdf"


def test_audio_and_video_get_text_placeholder():
    """Regression for #53: unsupported blocks must not be silently dropped."""
    out = _convert_messages(
        [
            Message(
                role="user",
                content=[
                    AudioBlock(source="file:///tmp/a.mp3", format="mp3"),
                    VideoBlock(source="file:///tmp/v.mp4"),
                ],
            )
        ]
    )
    texts = [p.text for p in out[0].parts]
    assert len(texts) == 2
    assert "Audio not supported by Gemini" in texts[0]
    assert "Video not supported by Gemini" in texts[1]


# ─── Message conversion: tool blocks ─────────────────────────────────────────


def test_tool_use_block_becomes_function_call_part():
    out = _convert_messages(
        [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        tool_call_id="t1", tool_name="bash", args={"cmd": "ls"}
                    ),
                ],
            )
        ]
    )
    part = out[0].parts[0]
    assert part.function_call is not None
    assert part.function_call.name == "bash"
    assert part.function_call.args == {"cmd": "ls"}
    assert part.function_call.id == "t1"


def test_tool_result_block_becomes_function_response_part():
    """A ToolResultBlock only carries the tool_call_id; the name must be
    resolved from the preceding assistant ToolUseBlock."""
    out = _convert_messages(
        [
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        tool_call_id="t1",
                        tool_name="get_weather",
                        args={"city": "Paris"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_call_id="t1", result="sunny", error=False),
                ],
            ),
        ]
    )
    # system/user text → user, assistant tool use → model, result → user
    assert out[1].role == "model"
    response_part = out[2].parts[0]
    assert response_part.function_response is not None
    assert response_part.function_response.name == "get_weather"
    assert response_part.function_response.id == "t1"
    assert response_part.function_response.response == {"result": "sunny"}


def test_tool_result_unknown_call_id_gets_empty_name():
    out = _convert_messages(
        [
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_call_id="nope", result="x", error=False),
                ],
            )
        ]
    )
    part = out[0].parts[0]
    assert part.function_response is not None
    assert part.function_response.name == ""
    assert part.function_response.id == "nope"


# ─── Streaming helpers ───────────────────────────────────────────────────────


def _chunk(parts, finish_reason=None, usage=None):
    """Build a GenerateContentResponse with one candidate holding *parts*."""
    candidate = types.Candidate(
        content=types.Content(role="model", parts=parts),
        finish_reason=finish_reason,
    )
    return types.GenerateContentResponse(
        candidates=[candidate],
        usage_metadata=usage,
    )


def _streaming_client(chunks):
    """Build a fake google-genai client whose
    ``aio.models.generate_content_stream`` returns *chunks* (a coroutine
    returning an async iterable, like the real SDK)."""

    class _AsyncIter:
        def __init__(self, items):
            self._iter = iter(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class FakeModels:
        def __init__(self, chunks):
            self._chunks = chunks
            self.calls = []

        async def generate_content_stream(self, **kwargs):
            self.calls.append(kwargs)
            return _AsyncIter(self._chunks)

    class FakeAio:
        def __init__(self, chunks):
            self.models = FakeModels(chunks)

    class FakeClient:
        def __init__(self, chunks):
            self.aio = FakeAio(chunks)

    return FakeClient(chunks)


# ─── Streaming: multi-tool ───────────────────────────────────────────────────


async def test_gemini_stream_multi_tool_calls_get_distinct_indices():
    """Two function_call parts in one response must produce two
    ToolCallEvents with distinct indices (regression: index was hardcoded 0)."""
    chunks = [
        _chunk(
            [
                types.Part(
                    function_call=types.FunctionCall(
                        id="fc1", name="get_weather", args={"city": "Paris"}
                    )
                ),
                types.Part(
                    function_call=types.FunctionCall(
                        id="fc2", name="search", args={"q": "news"}
                    )
                ),
            ],
            finish_reason=types.FinishReason.STOP,
        )
    ]

    chat = GeminiChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="gemini-2.0-flash", temperature=0.1, max_tokens=100)
    tools = [
        ToolDefinition(
            name="get_weather", description="w", parameters={"type": "object"}
        ),
        ToolDefinition(name="search", description="s", parameters={"type": "object"}),
    ]

    events = [
        e async for e in chat.stream([Message(role="user", content="hi")], cfg, tools)
    ]

    assert isinstance(events[0], LLMStartEvent)
    assert isinstance(events[-1], LLMDoneEvent)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 2
    assert [e.index for e in tool_events] == [0, 1]
    assert tool_events[0].tool_call_id == "fc1"
    assert tool_events[0].tool_name == "get_weather"
    assert tool_events[0].args == {"city": "Paris"}
    assert tool_events[1].tool_call_id == "fc2"
    assert tool_events[1].tool_name == "search"
    assert tool_events[1].args == {"q": "news"}

    done = events[-1]
    assert done.has_tool_calls is True
    assert done.stop_reason == "end_turn"


async def test_gemini_stream_function_call_without_id_gets_synthesized_id():
    chunks = [
        _chunk(
            [
                types.Part(
                    function_call=types.FunctionCall(name="bash", args={"cmd": "ls"})
                )
            ],
            finish_reason=types.FinishReason.STOP,
        )
    ]

    chat = GeminiChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    tool = next(e for e in events if isinstance(e, ToolCallEvent))
    assert tool.index == 0
    assert tool.tool_call_id == "call_0"
    assert tool.tool_name == "bash"


async def test_gemini_stream_text_and_usage():
    chunks = [
        _chunk([types.Part.from_text(text="Hello ")]),
        _chunk(
            [types.Part.from_text(text="world")],
            finish_reason=types.FinishReason.STOP,
            usage=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=7, candidates_token_count=3
            ),
        ),
    ]

    chat = GeminiChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert "".join(t.content for t in tokens) == "Hello world"

    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].usage.input == 7
    assert usage_events[0].usage.output == 3

    done = events[-1]
    assert isinstance(done, LLMDoneEvent)
    assert done.content == "Hello world"
    assert done.has_tool_calls is False


async def test_gemini_stream_passes_tools_in_config():
    chunks = [
        _chunk([types.Part.from_text(text="ok")], finish_reason=types.FinishReason.STOP)
    ]

    chat = GeminiChat(api_key="test-key")
    chat._client = _streaming_client(chunks)
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)
    tools = [
        ToolDefinition(name="bash", description="run", parameters={"type": "object"})
    ]

    async for _ in chat.stream([Message(role="user", content="hi")], cfg, tools):
        pass

    call = chat._client.aio.models.calls[0]
    assert call["model"] == "m"
    # tools must reach the GenerateContentConfig
    assert call["config"].tools is not None
    decl = call["config"].tools[0].function_declarations[0]
    assert decl.name == "bash"


# ─── Error handling ──────────────────────────────────────────────────────────


async def test_gemini_emits_error_event_on_exception():
    class FakeModels:
        async def generate_content_stream(self, **kwargs):
            raise RuntimeError("boom")

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    chat = GeminiChat(api_key="test-key")
    chat._client = FakeClient()
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    assert isinstance(events[-1], ErrorEvent)
    assert "boom" in events[-1].message
    assert events[-1].code == "provider_error"
