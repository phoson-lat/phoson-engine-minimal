"""Tests for #177 — retry wired into the CLI's build_chat (F-12).

The streaming semantics (retry only before the first user-visible token,
no duplication, max-attempts give-up) are covered by
``tests/phoson_llm/test_retry.py`` for ``RetryingChat`` itself; these
tests pin the *integration*: ``build_chat`` actually wraps every
provider's adapter, honours ``llm_max_attempts`` (1 disables), and a
429 before the first token is retried to success end-to-end.
"""

import sys
import asyncio
from pathlib import Path
from collections.abc import AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phoson_llm.retry import (
    RetryingChat,
    with_retry,  # noqa: F401  (re-export sanity)
)
from phoson_cli.config import PhosonConfig, build_chat
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


def _ollama_config(**overrides) -> PhosonConfig:
    """A config whose provider needs no credential, so build_chat works."""
    return PhosonConfig(provider="ollama", **overrides)


class _FailOnceThenSucceed(BaseLLMChat):
    """First stream errors (429, retryable); second one succeeds."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        yield LLMStartEvent(model="m", message_count=1)
        if self.calls == 1:
            yield ErrorEvent(message="rate limited", code="rate_limit", retryable=True)
            return
        yield TokenEvent(content="ok")
        yield UsageEvent(
            model="m",
            usage=TokenUsage(input=1, output=1),
            cost_usd=0.0,
            cost_known=False,
        )
        yield LLMDoneEvent(content="ok", has_tool_calls=False)


class _AlwaysFail(BaseLLMChat):
    """Every stream errors with a retryable 429."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        yield LLMStartEvent(model="m", message_count=1)
        yield ErrorEvent(message="rate limited", code="rate_limit", retryable=True)


class _FailAfterToken(BaseLLMChat):
    """Emits a token, then a retryable error — must NOT retry (no dup)."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        yield LLMStartEvent(model="m", message_count=1)
        yield TokenEvent(content="half")
        yield ErrorEvent(message="dropped", code="net", retryable=True)


def _no_sleep(monkeypatch) -> None:
    """Zero out the backoff sleep so tests run instantly."""

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


# ── build_chat wraps the adapter ─────────────────────────────────────────────


def test_build_chat_wraps_adapter_in_retrying_chat() -> None:
    chat = build_chat(_ollama_config(llm_max_attempts=3))
    assert isinstance(chat, RetryingChat)
    # The wrapper exposes the configured attempt budget.
    assert chat._policy.max_attempts == 3


def test_build_chat_max_attempts_one_is_bare_adapter() -> None:
    """llm_max_attempts=1 disables retries: no wrapper, bare adapter."""
    chat = build_chat(_ollama_config(llm_max_attempts=1))
    assert not isinstance(chat, RetryingChat)


def test_build_chat_default_is_three_attempts() -> None:
    chat = build_chat(_ollama_config())
    assert isinstance(chat, RetryingChat)
    assert chat._policy.max_attempts == 3


def test_build_chat_honours_env_override(monkeypatch) -> None:
    from phoson_cli.config import load_config

    monkeypatch.setenv("PHOSON_LLM_MAX_ATTEMPTS", "5")
    config = load_config()
    assert config.llm_max_attempts == 5
    chat = build_chat(config)
    assert isinstance(chat, RetryingChat)
    assert chat._policy.max_attempts == 5


def test_llm_max_attempts_config_roundtrip(monkeypatch) -> None:
    from phoson_cli.config import load_config

    monkeypatch.setenv("PHOSON_LLM_MAX_ATTEMPTS", "1")
    assert load_config().llm_max_attempts == 1


# ── end-to-end: 429 before the first token is retried ────────────────────────


@pytest.mark.asyncio
async def test_429_before_first_token_is_retried_to_success(monkeypatch) -> None:
    """The issue's headline case: a rate-limit before any token no longer
    kills the turn — the second attempt delivers the answer."""
    _no_sleep(monkeypatch)
    inner = _FailOnceThenSucceed()
    chat = with_retry(inner, max_attempts=3, initial_delay=0, jitter=0.0)

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")], ModelConfig(model="m", max_tokens=8)
        )
    ]

    assert inner.calls == 2  # retried once
    assert any(isinstance(e, LLMDoneEvent) and e.content == "ok" for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_no_retry_after_first_token_no_duplication(monkeypatch) -> None:
    """A committed stream (token already emitted) is never re-run."""
    _no_sleep(monkeypatch)
    inner = _FailAfterToken()
    chat = with_retry(inner, max_attempts=3, initial_delay=0, jitter=0.0)

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")], ModelConfig(model="m", max_tokens=8)
        )
    ]

    assert inner.calls == 1  # not retried
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert len(tokens) == 1  # no duplicated output
    assert any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    inner = _AlwaysFail()
    chat = with_retry(inner, max_attempts=3, initial_delay=0, jitter=0.0)

    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")], ModelConfig(model="m", max_tokens=8)
        )
    ]

    assert inner.calls == 3  # initial + 2 retries
    # The final error surfaces to the caller.
    assert any(isinstance(e, ErrorEvent) and e.retryable for e in events)
