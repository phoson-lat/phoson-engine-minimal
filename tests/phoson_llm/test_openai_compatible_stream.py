"""Tests for the shared OpenAI-compatible streaming loop.

Both ``OpenAIChat`` and ``OpenRouterChat`` now delegate to
``stream_chat_completions`` in :mod:`phoson_llm.chats._openai_compatible`.
These tests exercise the shared loop directly with a fake ``AsyncOpenAI``
client so the protocol-level behaviour is locked in regardless of which
adapter calls it.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import field, dataclass

import pytest

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ReasoningDoneEvent,
    ToolCallDeltaEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.chats._openai_compatible import stream_chat_completions


# ─── Fake OpenAI SDK objects ─────────────────────────────────────────────────


@dataclass
class _Function:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ToolCallDelta:
    index: int
    id: str | None = None
    function: _Function | None = None


@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_ToolCallDelta] | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _PromptDetails:
    cached_tokens: int = 0


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _PromptDetails = field(default_factory=_PromptDetails)


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
        for c in self._chunks:
            yield c


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


async def _collect(it: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [ev async for ev in it]


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_text_streaming_emits_typed_events() -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="Hi "))]),
        _Chunk(choices=[_Choice(delta=_Delta(content="there"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    client = _FakeClient(chunks)
    config = ModelConfig(model="gpt-4o-mini", max_tokens=128)

    events = await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=config,
        )
    )

    types = [type(e).__name__ for e in events]
    assert types[0] == "LLMStartEvent"
    assert types.count("TokenEvent") == 2
    assert "UsageEvent" in types
    assert types[-1] == "LLMDoneEvent"

    done = next(e for e in events if isinstance(e, LLMDoneEvent))
    assert done.content == "Hi there"
    assert done.has_tool_calls is False


@pytest.mark.asyncio
async def test_max_tokens_key_is_forwarded() -> None:
    """The shared loop must honour the per-provider max_tokens field name."""
    client = _FakeClient([])
    config = ModelConfig(model="gpt-4o-mini", max_tokens=999)

    await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=config,
            max_tokens_key="max_completion_tokens",
        )
    )

    sent = client.chat.completions.last_kwargs
    assert sent is not None
    assert sent.get("max_completion_tokens") == 999
    assert "max_tokens" not in sent


@pytest.mark.asyncio
async def test_system_message_is_normalised_to_head() -> None:
    client = _FakeClient([])
    config = ModelConfig(model="gpt-4o-mini", max_tokens=64, system="be terse")

    await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[
                Message(role="system", content="ignored"),
                Message(role="user", content="hi"),
            ],
            config=config,
        )
    )

    sent = client.chat.completions.last_kwargs
    assert sent is not None
    msgs = sent["messages"]
    # The system pulled from config replaces any inline system message.
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1]["role"] == "user"
    assert all(m.get("role") != "system" for m in msgs[1:])


@pytest.mark.asyncio
async def test_reasoning_effort_drops_temperature() -> None:
    client = _FakeClient([])
    config = ModelConfig(
        model="gpt-4o-mini",
        max_tokens=64,
        temperature=0.5,
        reasoning_effort="medium",
    )

    await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=config,
        )
    )

    sent = client.chat.completions.last_kwargs
    assert sent is not None
    assert sent["reasoning_effort"] == "medium"
    assert "temperature" not in sent


@pytest.mark.asyncio
async def test_tool_call_emission_is_idempotent() -> None:
    """Tool args streamed across chunks must be aggregated and emitted once."""
    chunks = [
        _Chunk(
            choices=[
                _Choice(
                    delta=_Delta(
                        tool_calls=[
                            _ToolCallDelta(
                                index=0,
                                id="call_1",
                                function=_Function(
                                    name="get_weather", arguments='{"city":'
                                ),
                            )
                        ]
                    )
                )
            ]
        ),
        _Chunk(
            choices=[
                _Choice(
                    delta=_Delta(
                        tool_calls=[
                            _ToolCallDelta(
                                index=0,
                                function=_Function(arguments='"Querétaro"}'),
                            )
                        ]
                    )
                )
            ]
        ),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="tool_calls")]),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=4)),
    ]
    client = _FakeClient(chunks)
    config = ModelConfig(model="gpt-4o-mini", max_tokens=64)
    tools = [
        ToolDefinition(
            name="get_weather",
            description="weather",
            parameters={"type": "object"},
        )
    ]

    events = await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="weather?")],
            config=config,
            tools=tools,
        )
    )

    tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "get_weather"
    assert tool_calls[0].args == {"city": "Querétaro"}

    done = next(e for e in events if isinstance(e, LLMDoneEvent))
    assert done.has_tool_calls is True


@pytest.mark.asyncio
async def test_reasoning_channel_is_emitted_via_either_attribute() -> None:
    chunks_with_alias = [
        _Chunk(choices=[_Choice(delta=_Delta(content="", reasoning_content="step "))]),
        _Chunk(
            choices=[_Choice(delta=_Delta(content="", reasoning_content="by step"))]
        ),
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]
    client = _FakeClient(chunks_with_alias)
    config = ModelConfig(model="gpt-4o-mini", max_tokens=64)

    events = await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=config,
        )
    )

    types = [type(e).__name__ for e in events]
    assert types.count("ReasoningStartEvent") == 1
    assert types.count("ReasoningTokenEvent") == 2
    assert types.count("ReasoningDoneEvent") == 1
    done = next(e for e in events if isinstance(e, ReasoningDoneEvent))
    assert done.content == "step by step"


@pytest.mark.asyncio
async def test_cost_calculator_is_invoked_with_token_counts() -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
        _Chunk(
            usage=_Usage(
                prompt_tokens=11,
                completion_tokens=22,
                prompt_tokens_details=_PromptDetails(cached_tokens=3),
            )
        ),
    ]
    client = _FakeClient(chunks)
    config = ModelConfig(model="gpt-4o-mini", max_tokens=64)

    seen: dict[str, object] = {}

    def fake_cost(
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
    ) -> tuple[float, bool]:
        seen.update(
            model=model,
            input=input_tokens,
            output=output_tokens,
            cache_read=cache_read_tokens,
        )
        return (0.123, True)

    events = await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=config,
            cost_calculator=fake_cost,
        )
    )

    assert seen == {
        "model": "gpt-4o-mini",
        "input": 11,
        "output": 22,
        "cache_read": 3,
    }
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert usage.cost_usd == 0.123
    assert usage.cost_known is True


@pytest.mark.asyncio
async def test_default_cost_callback_marks_cost_unknown() -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="ok"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
        _Chunk(usage=_Usage(prompt_tokens=1, completion_tokens=1)),
    ]
    client = _FakeClient(chunks)
    config = ModelConfig(model="any", max_tokens=8)

    events = await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=config,
        )
    )

    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert usage.cost_known is False
    assert usage.cost_usd == 0.0


# ─── Error mapping ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_status_5xx_is_retryable() -> None:
    from openai import APIStatusError

    class _BoomCompletions:
        async def create(self, **kwargs: object) -> _FakeStream:
            req = type("Req", (), {})()
            body = {"error": {"message": "upstream blew up"}}
            raise APIStatusError(
                "boom",
                response=type(
                    "Resp",
                    (),
                    {"status_code": 503, "request": req, "headers": {}},
                )(),
                body=body,
            )

    client = _FakeClient([])
    client.chat.completions = _BoomCompletions()  # type: ignore[assignment]

    events = await _collect(
        stream_chat_completions(
            client,  # type: ignore[arg-type]
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="any", max_tokens=8),
        )
    )

    err = next(e for e in events if isinstance(e, ErrorEvent))
    assert err.retryable is True
