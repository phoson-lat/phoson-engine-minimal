"""Unit tests for phoson_agent.middleware — AgentMiddleware and RetryMiddleware."""

import pytest

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
)
from phoson_agent.middleware import AgentMiddleware, RetryMiddleware

# ── helpers ──────────────────────────────────────────────────────────────────


def _config() -> ModelConfig:
    return ModelConfig(model="test-model")


def _msgs() -> list[Message]:
    return [Message(role="user", content="hi")]


async def _stream(*events):
    """Async generator that yields the given events."""
    for e in events:
        yield e


async def _collect(ait) -> list:
    return [e async for e in ait]


# ── AgentMiddleware base passthrough ─────────────────────────────────────────


class TestAgentMiddlewareBase:
    @pytest.mark.asyncio
    async def test_on_before_llm_returns_messages_unchanged(self):
        mw = AgentMiddleware()
        msgs = _msgs()
        result = await mw.on_before_llm(msgs, _config())
        assert result is msgs

    @pytest.mark.asyncio
    async def test_wrap_llm_call_passes_events_through(self):
        mw = AgentMiddleware()
        events = [TokenEvent(content="a"), LLMDoneEvent()]

        async def call_next(messages, config):
            async for e in _stream(*events):
                yield e

        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))
        assert len(result) == 2
        assert isinstance(result[0], TokenEvent)

    @pytest.mark.asyncio
    async def test_on_before_tool_returns_call_unchanged(self):
        from phoson_llm.schemas import ToolCallEvent

        mw = AgentMiddleware()
        call = ToolCallEvent(tool_call_id="c1", tool_name="search")
        result = await mw.on_before_tool(call)
        assert result is call

    @pytest.mark.asyncio
    async def test_on_after_tool_returns_result_unchanged(self):
        from phoson_llm.schemas import ToolCallEvent

        mw = AgentMiddleware()
        call = ToolCallEvent(tool_call_id="c1", tool_name="search")
        result = await mw.on_after_tool(call, "found it", False)
        assert result == "found it"

    @pytest.mark.asyncio
    async def test_on_agent_event_returns_none(self):
        from phoson_agent.models import AgentStartEvent

        mw = AgentMiddleware()
        result = await mw.on_agent_event(AgentStartEvent())
        assert result is None


# ── RetryMiddleware ──────────────────────────────────────────────────────────


class TestRetryMiddleware:
    @pytest.mark.asyncio
    async def test_passes_through_on_success(self):
        mw = RetryMiddleware(max_retries=2)
        events = [TokenEvent(content="ok"), LLMDoneEvent()]

        async def call_next(messages, config):
            async for e in _stream(*events):
                yield e

        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self):
        call_count = 0

        async def call_next(messages, config):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                yield ErrorEvent(message="rate limit", retryable=True)
            else:
                yield TokenEvent(content="success")
                yield LLMDoneEvent()

        mw = RetryMiddleware(max_retries=2, base_delay_seconds=0)
        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))

        assert call_count == 2
        assert any(isinstance(e, TokenEvent) for e in result)

    @pytest.mark.asyncio
    async def test_does_not_retry_non_retryable_error(self):
        call_count = 0

        async def call_next(messages, config):
            nonlocal call_count
            call_count += 1
            yield ErrorEvent(message="auth error", retryable=False)

        mw = RetryMiddleware(max_retries=3, base_delay_seconds=0)
        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))

        assert call_count == 1
        assert isinstance(result[0], ErrorEvent)

    @pytest.mark.asyncio
    async def test_yields_error_after_max_retries_exhausted(self):
        async def call_next(messages, config):
            yield ErrorEvent(message="overloaded", retryable=True)

        mw = RetryMiddleware(max_retries=2, base_delay_seconds=0)
        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))

        # Called 3 times (initial + 2 retries), last error is yielded
        assert isinstance(result[-1], ErrorEvent)
        assert result[-1].message == "overloaded"

    @pytest.mark.asyncio
    async def test_does_not_retry_after_visible_events(self):
        """If tokens were already emitted, a later error is not retried."""
        call_count = 0

        async def call_next(messages, config):
            nonlocal call_count
            call_count += 1
            yield TokenEvent(content="partial")
            yield ErrorEvent(message="mid-stream error", retryable=True)

        mw = RetryMiddleware(max_retries=3, base_delay_seconds=0)
        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))

        assert call_count == 1  # no retry after partial output
        assert isinstance(result[0], TokenEvent)
        assert isinstance(result[1], ErrorEvent)

    @pytest.mark.asyncio
    async def test_backoff_delay_is_applied(self, monkeypatch):
        import asyncio

        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        attempt = 0

        async def call_next(messages, config):
            nonlocal attempt
            attempt += 1
            if attempt <= 2:
                yield ErrorEvent(message="retry me", retryable=True)
            else:
                yield LLMDoneEvent()

        mw = RetryMiddleware(
            max_retries=3, base_delay_seconds=1.0, backoff_multiplier=2.0
        )
        await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))

        assert len(delays) == 2
        assert delays[0] == pytest.approx(1.0)
        assert delays[1] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_custom_max_retries_zero_means_no_retry(self):
        call_count = 0

        async def call_next(messages, config):
            nonlocal call_count
            call_count += 1
            yield ErrorEvent(message="fail", retryable=True)

        mw = RetryMiddleware(max_retries=0, base_delay_seconds=0)
        result = await _collect(mw.wrap_llm_call(call_next, _msgs(), _config()))

        assert call_count == 1
        assert isinstance(result[-1], ErrorEvent)
