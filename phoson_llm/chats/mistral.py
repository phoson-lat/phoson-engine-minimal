"""Mistral AI adapter using the native SDK."""

import os
from typing import TYPE_CHECKING
from collections.abc import AsyncIterator

from phoson_llm.utils import normalize_stop_reason
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
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat

if TYPE_CHECKING:
    from mistralai.client import Mistral


class MistralChat(BaseLLMChat):
    """Adapter for Mistral AI API using the ``mistralai`` SDK.

    Args:
        api_key: Mistral API key. Defaults to ``MISTRAL_API_KEY`` env var.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("MISTRAL_API_KEY") or ""
        self._client = None

    def _get_client(self) -> "Mistral":
        if self._client is None:
            from mistralai.client import Mistral

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
        # F-13: Mistral (OpenAI-compatible) reports the stop reason on the
        # choice as ``finish_reason``. Probe defensively — the SDK shape has
        # varied, and a missing attribute must not break the stream.
        stop_reason: object = None

        try:
            stream_response = await client.chat.stream_async(
                model=config.model,
                messages=mistral_messages,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                # tools=...
            )

            async for chunk in stream_response:
                choice = chunk.data.choices[0]
                delta_content = choice.delta.content
                if isinstance(delta_content, str) and delta_content:
                    text_acc += delta_content
                    yield TokenEvent(content=delta_content)

                finish = getattr(choice, "finish_reason", None)
                if finish is not None:
                    stop_reason = finish

                if chunk.data.usage:
                    u = chunk.data.usage
                    usage = TokenUsage(
                        input=u.prompt_tokens or 0,
                        output=u.completion_tokens or 0,
                    )
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

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
            stop_reason=normalize_stop_reason(stop_reason, provider="openai_compat"),
        )
