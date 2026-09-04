"""Tests for #178 — normalized stop_reason across every adapter (F-13).

Every adapter must expose a ``LLMDoneEvent.stop_reason`` normalized to the
Phoson vocabulary (``end_turn`` / ``max_tokens`` / ``tool_use`` / ``refusal`` /
``pause_turn`` / ``other``). The OpenAI-compatible shared loop covers 15 of the
19 adapters, so it is tested once here; the five independent stream
implementations (anthropic, ollama, bedrock, gemini, mistral) each get a
per-adapter fake-stream test.

The tests use fakes that mirror the SDK shapes the adapters actually touch, so
they lock the *wiring* (which field the adapter reads, and which provider table
normalizes it) rather than re-testing ``normalize_stop_reason`` in isolation.
"""

from dataclasses import field, dataclass
from collections.abc import AsyncIterator

import pytest

from phoson_llm.utils import STOP_REASONS, normalize_stop_reason
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ModelConfig,
    LLMDoneEvent,
)

try:
    import google.genai as _google  # noqa: F401

    _HAS_GOOGLE = True
except ImportError:  # pragma: no cover - google-genai is an optional dep
    _HAS_GOOGLE = False


def _done(events: list[LLMEvent]) -> LLMDoneEvent:
    """Return the LLMDoneEvent in a collected event list (exactly one)."""
    dones = [e for e in events if isinstance(e, LLMDoneEvent)]
    assert len(dones) == 1, f"expected 1 LLMDoneEvent, got {dones}"
    return dones[0]


# ─── normalize_stop_reason unit coverage ─────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "provider", "expected"),
    [
        # OpenAI-compatible
        ("stop", "openai_compat", "end_turn"),
        ("length", "openai_compat", "max_tokens"),
        ("tool_calls", "openai_compat", "tool_use"),
        ("function_call", "openai_compat", "tool_use"),
        ("content_filter", "openai_compat", "refusal"),
        ("length", "openai_compat", "max_tokens"),
        ("weird_reason", "openai_compat", "other"),
        # Anthropic
        ("end_turn", "anthropic", "end_turn"),
        ("stop_sequence", "anthropic", "end_turn"),
        ("max_tokens", "anthropic", "max_tokens"),
        ("tool_use", "anthropic", "tool_use"),
        ("refusal", "anthropic", "refusal"),
        ("pause_turn", "anthropic", "pause_turn"),
        # Ollama
        ("stop", "ollama", "end_turn"),
        ("length", "ollama", "max_tokens"),
        ("tool_calls", "ollama", "tool_use"),
        # Bedrock
        ("end_turn", "bedrock", "end_turn"),
        ("stop_sequence", "bedrock", "end_turn"),
        ("max_tokens", "bedrock", "max_tokens"),
        ("tool_use", "bedrock", "tool_use"),
        # Gemini (enum name, case-insensitive)
        ("STOP", "google", "end_turn"),
        ("MAX_TOKENS", "google", "max_tokens"),
        ("SAFETY", "google", "refusal"),
        ("max_tokens", "google", "max_tokens"),  # lower-case also accepted
        # None / empty
        (None, "openai_compat", None),
        ("", "openai_compat", None),
        ("   ", "openai_compat", None),
    ],
)
def test_normalize_stop_reason(
    raw: str | None, provider: str, expected: str | None
) -> None:
    assert normalize_stop_reason(raw, provider=provider) == expected


def test_normalize_stop_reason_gemini_enum_object() -> None:
    """Accept a Gemini FinishReason enum object (its ``.name`` is used)."""

    class _FakeFinishReason:
        name = "MAX_TOKENS"

    assert normalize_stop_reason(_FakeFinishReason(), provider="google") == "max_tokens"


def test_stop_reasons_vocabulary_is_stable() -> None:
    assert STOP_REASONS == (
        "end_turn",
        "max_tokens",
        "tool_use",
        "refusal",
        "pause_turn",
        "other",
    )


# ─── OpenAI-compatible shared loop ───────────────────────────────────────────
# (covers OpenAI, OpenRouter, Azure, Cohere, DeepSeek, Fireworks, Grok, Groq,
#  LM Studio, NVIDIA, Perplexity, Together, vLLM — 15 adapters)


@dataclass
class _OC_Function:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _OC_ToolCallDelta:
    index: int
    id: str | None = None
    function: _OC_Function | None = None


@dataclass
class _OC_Delta:
    content: str | None = None
    tool_calls: list[_OC_ToolCallDelta] | None = None


@dataclass
class _OC_Choice:
    delta: _OC_Delta
    finish_reason: str | None = None


@dataclass
class _OC_Chunk:
    choices: list[_OC_Choice] = field(default_factory=list)
    usage: object = None


class _OC_FakeStream:
    def __init__(self, chunks: list[_OC_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> AsyncIterator[_OC_Chunk]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_OC_Chunk]:
        for c in self._chunks:
            yield c


class _OC_FakeCompletions:
    def __init__(self, chunks: list[_OC_Chunk]) -> None:
        self._chunks = chunks

    async def create(self, **kwargs: object) -> _OC_FakeStream:
        return _OC_FakeStream(self._chunks)


class _OC_FakeClient:
    def __init__(self, chunks: list[_OC_Chunk]) -> None:
        self.chat = type("Chat", (), {"completions": _OC_FakeCompletions(chunks)})


def _oc_client(chunks: list[_OC_Chunk]) -> _OC_FakeClient:
    return _OC_FakeClient(chunks)


@pytest.mark.asyncio
async def test_openai_compat_reports_end_turn() -> None:
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(content="hello"))]),
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(), finish_reason="stop")]),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_openai_compat_reports_max_tokens() -> None:
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(content="trunc"))]),
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(), finish_reason="length")]),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_openai_compat_reports_tool_use_on_tool_calls_finish() -> None:
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        _OC_Chunk(
            choices=[
                _OC_Choice(
                    _OC_Delta(
                        tool_calls=[
                            _OC_ToolCallDelta(
                                index=0,
                                id="call_1",
                                function=_OC_Function(name="echo", arguments='{"x":1}'),
                            )
                        ]
                    )
                )
            ]
        ),
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(), finish_reason="tool_calls")]),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    done = _done(events)
    assert done.stop_reason == "tool_use"
    assert done.has_tool_calls is True


@pytest.mark.asyncio
async def test_openai_compat_unknown_finish_reason_is_other() -> None:
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(content="x"))]),
        _OC_Chunk(
            choices=[_OC_Choice(_OC_Delta(), finish_reason="some_future_reason")]
        ),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "other"


@pytest.mark.asyncio
async def test_openai_compat_no_finish_reason_is_none() -> None:
    """A stream that ends without a finish signal leaves stop_reason unset
    (None) rather than inventing a value."""
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(content="just text"))]),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason is None


# ─── OpenAI-compatible truncation: partial tool call on max_tokens ───────────
# (acceptance criterion: "stream con stop_reason=max_tokens y JSON parcial →
#  el handler no se invoca; el modelo recibe un error accionable")


@pytest.mark.asyncio
async def test_openai_compat_max_tokens_truncated_tool_call_is_emitted() -> None:
    """A tool call cut off by max_tokens (never got a tool_calls finish) must
    be emitted as a truncated ToolCallEvent so the loop answers it instead of
    leaving an orphaned tool_use."""
    from phoson_llm.schemas import ToolCallEvent
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        # A tool call with a *partial* JSON arg, cut off before any finish.
        _OC_Chunk(
            choices=[
                _OC_Choice(
                    _OC_Delta(
                        tool_calls=[
                            _OC_ToolCallDelta(
                                index=0,
                                id="call_1",
                                function=_OC_Function(
                                    name="bash", arguments='{"command": "ls'
                                ),
                            )
                        ]
                    )
                )
            ]
        ),
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(), finish_reason="length")]),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="ls the dir")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    done = _done(events)
    assert done.stop_reason == "max_tokens"
    assert done.has_tool_calls is True

    tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    # Marked truncated so the agent loop answers it with an actionable error
    # result instead of invoking the handler on partial JSON.
    assert tc.args.get("_truncated") is True
    assert tc.tool_call_id == "call_1"
    assert tc.tool_name == "bash"


@pytest.mark.asyncio
async def test_openai_compat_end_turn_does_not_mark_truncated() -> None:
    """A normal tool turn (finish=tool_calls) must NOT be tagged truncated —
    only max_tokens triggers the truncated path."""
    from phoson_llm.schemas import ToolCallEvent
    from phoson_llm.chats._openai_compatible import stream_chat_completions

    chunks = [
        _OC_Chunk(
            choices=[
                _OC_Choice(
                    _OC_Delta(
                        tool_calls=[
                            _OC_ToolCallDelta(
                                index=0,
                                id="call_1",
                                function=_OC_Function(
                                    name="bash", arguments='{"command":"ls"}'
                                ),
                            )
                        ]
                    )
                )
            ]
        ),
        _OC_Chunk(choices=[_OC_Choice(_OC_Delta(), finish_reason="tool_calls")]),
    ]
    events = [
        e
        async for e in stream_chat_completions(
            _oc_client(chunks),
            messages=[Message(role="user", content="ls")],
            config=ModelConfig(model="m", max_tokens=64),
        )
    ]
    tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_calls) == 1
    assert tool_calls[0].args.get("_truncated") is not True


# ─── Anthropic ───────────────────────────────────────────────────────────────


class _Anthropic_Delta:
    def __init__(self, dtype, text=None, partial_json=None) -> None:
        self.type = dtype
        self.text = text
        self.partial_json = partial_json


class _Anthropic_Event:
    def __init__(self, etype, index=0, delta=None) -> None:
        self.type = etype
        self.index = index
        self.delta = delta


class _Anthropic_FinalMessage:
    def __init__(
        self, stop_reason: str | None, input_tokens=5, output_tokens=5
    ) -> None:
        self.stop_reason = stop_reason

        class _Usage:
            pass

        self.usage = _Usage()
        self.usage.input_tokens = input_tokens
        self.usage.output_tokens = output_tokens
        self.usage.cache_creation_input_tokens = 0
        self.usage.cache_read_input_tokens = 0


class _Anthropic_Stream:
    def __init__(self, events, final: _Anthropic_FinalMessage) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        async def _iter():
            for e in self._events:
                yield e

        return _iter()

    async def get_final_message(self):
        return self._final


@pytest.mark.parametrize(
    "raw",
    ["end_turn", "stop_sequence", "max_tokens", "tool_use", "refusal", "pause_turn"],
)
@pytest.mark.asyncio
async def test_anthropic_reports_stop_reason(raw: str) -> None:
    from phoson_llm.chats.anthropic import AnthropicChat

    chat = AnthropicChat(api_key="test")

    stream = _Anthropic_Stream(
        [
            _Anthropic_Event(
                "content_block_delta", 0, _Anthropic_Delta("text_delta", text="ok")
            )
        ],
        _Anthropic_FinalMessage(stop_reason=raw),
    )
    # Patch the client.messages.stream context-manager method.
    chat._client = _Anthropic_FakeClient(stream)

    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="claude", max_tokens=64),
        )
    ]
    expected = normalize_stop_reason(raw, provider="anthropic")
    assert _done(events).stop_reason == expected


class _Anthropic_FakeClient:
    def __init__(self, stream: _Anthropic_Stream) -> None:
        self.messages = self._Messages(stream)

    class _Messages:
        def __init__(self, stream) -> None:
            self._stream = stream

        def stream(self, **kwargs):
            return self._stream


# ─── Ollama ──────────────────────────────────────────────────────────────────


class _Ollama_Response:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aiter_bytes(self):  # for the error path
        yield b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Ollama_FakeClient:
    def __init__(self, response: _Ollama_Response) -> None:
        self._response = response

    def stream(self, method, url, json=None):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def _patch_ollama_httpx(
    monkeypatch: pytest.MonkeyPatch, response: _Ollama_Response
) -> None:
    import phoson_llm.chats.ollama as mod

    factory = lambda *a, **k: _Ollama_FakeClient(response)  # noqa: E731
    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_ollama_reports_done_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from phoson_llm.chats.ollama import OllamaChat

    # Final line carries done_reason="length" → max_tokens.
    lines = [
        _json.dumps({"message": {"content": "partial"}, "done": False}),
        _json.dumps(
            {"message": {"content": ""}, "done": True, "done_reason": "length"}
        ),
    ]
    _patch_ollama_httpx(monkeypatch, _Ollama_Response(lines))
    chat = OllamaChat(base_url="http://localhost:11434")
    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="llama3", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_ollama_reports_stop_reason_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from phoson_llm.chats.ollama import OllamaChat

    lines = [
        _json.dumps({"message": {"content": "done"}, "done": False}),
        _json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
    ]
    _patch_ollama_httpx(monkeypatch, _Ollama_Response(lines))
    chat = OllamaChat(base_url="http://localhost:11434")
    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="llama3", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "end_turn"


# ─── Bedrock ─────────────────────────────────────────────────────────────────


class _Bedrock_FakeClient:
    def __init__(self, response: dict) -> None:
        self._response = response

    def converse(self, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_bedrock_reports_stop_reason() -> None:
    from phoson_llm.chats.bedrock import BedrockChat

    chat = BedrockChat(region_name="us-east-1")
    chat._client = _Bedrock_FakeClient(
        {
            "output": {"message": {"content": [{"text": "bedrock reply"}]}},
            "usage": {"inputTokens": 3, "outputTokens": 4},
            "stop_reason": "max_tokens",
        }
    )
    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="claude-v3", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "max_tokens"


# ─── Gemini ──────────────────────────────────────────────────────────────────


class _Gemini_Part:
    def __init__(self, text=None) -> None:
        self.text = text
        self.function_call = None


class _Gemini_Content:
    def __init__(self, parts) -> None:
        self.parts = parts


class _Gemini_Candidate:
    def __init__(self, parts, finish_reason) -> None:
        self.content = _Gemini_Content(parts)
        self.finish_reason = finish_reason


class _Gemini_Chunk:
    def __init__(self, candidates=None, usage_metadata=None) -> None:
        self.candidates = candidates
        self.usage_metadata = usage_metadata


class _Gemini_FakeAioModel:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    async def generate_content_stream(self, model=None, contents=None, config=None):
        async def _iter():
            for c in self._chunks:
                yield c

        return _iter()


class _Gemini_FakeClient:
    def __init__(self, chunks) -> None:
        self.aio = self._Aio()
        self.aio.models = _Gemini_FakeAioModel(chunks)

    class _Aio:
        pass


@pytest.mark.skipif(not _HAS_GOOGLE, reason="google-genai not installed (optional dep)")
@pytest.mark.asyncio
async def test_gemini_reports_finish_reason() -> None:
    from phoson_llm.chats.gemini import GeminiChat

    chat = GeminiChat(api_key="test")
    chat._client = _Gemini_FakeClient(
        [
            _Gemini_Chunk(
                candidates=[_Gemini_Candidate([_Gemini_Part("hello")], None)]
            ),
            _Gemini_Chunk(
                candidates=[_Gemini_Candidate([_Gemini_Part("")], "MAX_TOKENS")]
            ),
        ]
    )
    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="gemini", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "max_tokens"


# ─── Mistral ─────────────────────────────────────────────────────────────────


class _Mistral_Delta:
    def __init__(self, content=None) -> None:
        self.content = content


class _Mistral_Choice:
    def __init__(self, content=None, finish_reason=None) -> None:
        self.delta = _Mistral_Delta(content)
        self.finish_reason = finish_reason


class _Mistral_Data:
    def __init__(self, choices, usage=None) -> None:
        self.choices = choices
        self.usage = usage


class _Mistral_Chunk:
    def __init__(self, data) -> None:
        self.data = data


class _Mistral_FakeStreamResponse:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _iter():
            for c in self._chunks:
                yield c

        return _iter()


class _Mistral_FakeChat:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    async def stream_async(self, **kwargs):
        return _Mistral_FakeStreamResponse(self._chunks)


class _Mistral_FakeClient:
    def __init__(self, chunks) -> None:
        self.chat = _Mistral_FakeChat(chunks)


@pytest.mark.asyncio
async def test_mistral_reports_finish_reason() -> None:
    from phoson_llm.chats.mistral import MistralChat

    chat = MistralChat(api_key="test")
    chat._client = _Mistral_FakeClient(
        [
            _Mistral_Chunk(_Mistral_Data([_Mistral_Choice(content="hi")])),
            _Mistral_Chunk(
                _Mistral_Data([_Mistral_Choice(content=None, finish_reason="length")])
            ),
        ]
    )
    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="mistral", max_tokens=64),
        )
    ]
    assert _done(events).stop_reason == "max_tokens"
