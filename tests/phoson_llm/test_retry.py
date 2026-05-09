"""Tests for the retry / backoff wrapper."""

from collections.abc import AsyncIterator

import pytest

from phoson_llm.retry import RetryPolicy, RetryingChat, with_retry
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat


# ─── Fake adapter helpers ────────────────────────────────────────────────────


class _ScriptedChat(BaseLLMChat):
    """A chat that replays a list of pre-canned event sequences.

    Each call to ``stream`` consumes the next sequence in ``scripts``.
    Once the scripts are exhausted further calls raise to surface unexpected
    extra retries.
    """

    def __init__(self, scripts: list[list[LLMEvent]]) -> None:
        self._scripts = list(scripts)
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        if not self._scripts:
            raise AssertionError("No more scripted streams")
        events = self._scripts.pop(0)
        for ev in events:
            yield ev


def _success_stream(text: str = "ok") -> list[LLMEvent]:
    return [
        LLMStartEvent(model="m", message_count=1),
        TokenEvent(content=text),
        UsageEvent(
            model="m",
            usage=TokenUsage(input=1, output=1),
            cost_usd=0.0,
            cost_known=False,
        ),
        LLMDoneEvent(content=text, has_tool_calls=False),
    ]


def _error_stream(retryable: bool, code: str = "rate_limit") -> list[LLMEvent]:
    return [
        LLMStartEvent(model="m", message_count=1),
        ErrorEvent(message="boom", code=code, retryable=retryable),
    ]


def _no_jitter_policy(max_attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        initial_delay=0,
        max_delay=0,
        multiplier=1.0,
        jitter=0.0,
    )


# ─── compute_delay ───────────────────────────────────────────────────────────


def test_compute_delay_grows_geometrically_and_caps() -> None:
    policy = RetryPolicy(
        max_attempts=10,
        initial_delay=1,
        max_delay=10,
        multiplier=2.0,
        jitter=0.0,
    )
    assert policy.compute_delay(1) == 1
    assert policy.compute_delay(2) == 2
    assert policy.compute_delay(3) == 4
    assert policy.compute_delay(4) == 8
    # capped at max_delay
    assert policy.compute_delay(5) == 10
    assert policy.compute_delay(6) == 10


def test_compute_delay_is_zero_for_attempt_zero() -> None:
    policy = RetryPolicy(initial_delay=2.0, jitter=0.0)
    assert policy.compute_delay(0) == 0.0


# ─── RetryingChat ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passthrough_when_no_error() -> None:
    inner = _ScriptedChat([_success_stream("hello")])
    chat = RetryingChat(inner, _no_jitter_policy())

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]

    assert inner.calls == 1
    types = [type(e).__name__ for e in events]
    assert types == [
        "LLMStartEvent",
        "TokenEvent",
        "UsageEvent",
        "LLMDoneEvent",
    ]


@pytest.mark.asyncio
async def test_retries_on_retryable_error_before_tokens() -> None:
    inner = _ScriptedChat(
        [
            _error_stream(retryable=True),
            _error_stream(retryable=True),
            _success_stream("hello"),
        ]
    )
    chat = RetryingChat(inner, _no_jitter_policy(max_attempts=3))

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]

    assert inner.calls == 3
    # The successful stream's events appear at the end (after two retries
    # that yielded only LLMStartEvent before erroring out).
    assert any(isinstance(e, LLMDoneEvent) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_does_not_retry_when_error_is_not_retryable() -> None:
    inner = _ScriptedChat([_error_stream(retryable=False, code="auth")])
    chat = RetryingChat(inner, _no_jitter_policy(max_attempts=5))

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]

    assert inner.calls == 1
    err = next(e for e in events if isinstance(e, ErrorEvent))
    assert err.code == "auth"


@pytest.mark.asyncio
async def test_does_not_retry_after_user_visible_tokens() -> None:
    """If a TokenEvent has been forwarded, a later error must NOT retry."""
    committed_then_error: list[LLMEvent] = [
        LLMStartEvent(model="m", message_count=1),
        TokenEvent(content="partial "),
        ErrorEvent(message="conn dropped", code="connection_error", retryable=True),
    ]
    inner = _ScriptedChat([committed_then_error])
    chat = RetryingChat(inner, _no_jitter_policy(max_attempts=5))

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]

    # No retries — the partial output already reached the user.
    assert inner.calls == 1
    assert any(isinstance(e, TokenEvent) for e in events)
    assert any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts() -> None:
    inner = _ScriptedChat(
        [
            _error_stream(retryable=True),
            _error_stream(retryable=True),
            _error_stream(retryable=True),
        ]
    )
    chat = RetryingChat(inner, _no_jitter_policy(max_attempts=3))

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]

    assert inner.calls == 3
    err = next(e for e in events if isinstance(e, ErrorEvent))
    assert err.retryable is True


@pytest.mark.asyncio
async def test_with_retry_helper_returns_retrying_chat() -> None:
    inner = _ScriptedChat([_success_stream()])
    wrapped = with_retry(inner, max_attempts=2, initial_delay=0)
    assert isinstance(wrapped, RetryingChat)

    events = [
        ev
        async for ev in wrapped.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]
    assert any(isinstance(e, LLMDoneEvent) for e in events)


@pytest.mark.asyncio
async def test_max_attempts_one_disables_retries() -> None:
    inner = _ScriptedChat([_error_stream(retryable=True)])
    chat = RetryingChat(inner, _no_jitter_policy(max_attempts=1))

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")],
            ModelConfig(model="m", max_tokens=8),
        )
    ]

    assert inner.calls == 1
    assert any(isinstance(e, ErrorEvent) for e in events)
