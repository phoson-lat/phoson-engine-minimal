"""Mistral AI adapter using the native SDK."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)

if TYPE_CHECKING:
    from mistralai import Mistral


class MistralChat(BaseLLMChat):
    """Adapter for Mistral AI API using the ``mistralai`` SDK.

    Args:
        api_key: Mistral API key. Defaults to ``MISTRAL_API_KEY`` env var.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("MISTRAL_API_KEY") or ""
        self._client = None

    def _get_client(self) -> Mistral:
        if self._client is None:
            from mistralai import Mistral
            self._client = Mistral(api_key=self._api_key)
        return self._client

    def __repr__(self) -> str:
        return "MistralChat()"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        client = self._get_client()

        # Simple conversion for now
        mistral_messages = []
        for msg in messages:
            mistral_messages.append({"role": msg.role, "content": msg.content})

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        text_acc = ""
        has_tool_calls = False

        try:
            stream_response = await client.chat.stream_async(
                model=config.model,
                messages=mistral_messages,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                # tools=...
            )

            async for chunk in stream_response:
                if chunk.data.choices[0].delta.content:
                    content = chunk.data.choices[0].delta.content
                    text_acc += content
                    yield TokenEvent(content=content)
                
                if chunk.data.usage:
                    u = chunk.data.usage
                    usage = TokenUsage(input=u.prompt_tokens, output=u.completion_tokens)
                    cost_usd, cost_known = calculate_cost(
                        model=config.model,
                        input_tokens=usage.input,
                        output_tokens=usage.output,
                        provider="mistral",
                    )
                    yield UsageEvent(
                        model=config.model,
                        usage=usage,
                        cost_usd=cost_usd,
                        cost_known=cost_known,
                    )

        except Exception as e:
            from phoson_llm.schemas import ErrorEvent
            yield ErrorEvent(message=str(e), code="provider_error", retryable=False)
            return

        yield LLMDoneEvent(content=text_acc, has_tool_calls=has_tool_calls)
