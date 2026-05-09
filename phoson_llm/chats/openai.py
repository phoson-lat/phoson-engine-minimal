"""OpenAI Chat Completions adapter.

Thin wrapper around the shared OpenAI-compatible streaming loop in
:mod:`phoson_llm.chats._openai_compatible`. The adapter only owns the
client and the cost calculation; everything protocol-level is handled
by the shared loop.
"""

import os
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ModelConfig,
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats._openai_compatible import stream_chat_completions


def _openai_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> tuple[float, bool]:
    return calculate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        provider="openai",
    )


class OpenAIChat(BaseLLMChat):
    """Adapter for the OpenAI Chat Completions API.

    The adapter is intentionally tiny: it builds an ``AsyncOpenAI`` client
    and delegates streaming to the shared OpenAI-compatible loop. Use
    :class:`phoson_llm.chats.openrouter.OpenRouterChat` for OpenRouter
    rather than overriding ``base_url`` here.

    Supports streaming, tool calls, reasoning channels and multimodal
    inputs (images, audio, documents).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Initialize the OpenAI client.

        Args:
            api_key: OpenAI API key. Defaults to ``OPENAI_API_KEY``.
            base_url: Optional override for self-hosted gateways pointing
                at OpenAI's exact contract. For OpenRouter or Ollama use
                their dedicated adapters instead.
        """
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url,
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response from the OpenAI model."""
        async for event in stream_chat_completions(
            self._client,
            messages=messages,
            config=config,
            tools=tools,
            # Current OpenAI models reject `max_tokens` in favour of
            # `max_completion_tokens`. Reasoning models additionally reject
            # `temperature`, which the shared loop already handles.
            max_tokens_key="max_completion_tokens",
            cost_calculator=_openai_cost,
        ):
            yield event
