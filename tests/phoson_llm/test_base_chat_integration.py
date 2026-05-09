from collections.abc import AsyncIterator

import pytest

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.exceptions import PhosonProviderError


class FakeDoneChat(BaseLLMChat):
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield TokenEvent(content="hola")
        yield LLMDoneEvent(content="respuesta final", has_tool_calls=False)


class FakeErrorChat(BaseLLMChat):
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield ErrorEvent(message="rate limited", code="rate_limit", retryable=True)


@pytest.mark.asyncio
async def test_complete_returns_done_event() -> None:
    chat = FakeDoneChat()
    done = await chat.complete(
        messages=[Message(role="user", content="hola")],
        config=ModelConfig(model="fake-model"),
    )

    assert isinstance(done, LLMDoneEvent)
    assert done.content == "respuesta final"
    assert done.has_tool_calls is False


@pytest.mark.asyncio
async def test_complete_raises_for_error_event() -> None:
    chat = FakeErrorChat()

    with pytest.raises(PhosonProviderError, match=r"rate limited") as exc_info:
        await chat.complete(
            messages=[Message(role="user", content="hola")],
            config=ModelConfig(model="fake-model"),
        )

    assert exc_info.value.code == "rate_limit"
    assert exc_info.value.retryable is True


def test_stream_sync_yields_full_sequence() -> None:
    chat = FakeDoneChat()
    events = list(
        chat.stream_sync(
            messages=[Message(role="user", content="hola")],
            config=ModelConfig(model="fake-model"),
        )
    )

    assert [type(event) for event in events] == [
        LLMStartEvent,
        TokenEvent,
        LLMDoneEvent,
    ]


def test_complete_sync_returns_done_event() -> None:
    chat = FakeDoneChat()
    done = chat.complete_sync(
        messages=[Message(role="user", content="hola")],
        config=ModelConfig(model="fake-model"),
    )

    assert done.content == "respuesta final"
