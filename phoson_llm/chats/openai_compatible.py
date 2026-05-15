"""Generic adapter for any OpenAI-compatible Chat Completions API."""

import os
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from phoson_llm.schemas import Message, LLMEvent, ModelConfig, ToolDefinition
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats._openai_compatible import stream_chat_completions

if TYPE_CHECKING:
    from phoson_llm.chats._openai_compatible import CostCalculator


class OpenAICompatibleChat(BaseLLMChat):
    """Generic adapter for OpenAI-compatible Chat Completions APIs.

    Args:
        base_url: The provider's API base URL.
        api_key: API key. Falls back to ``api_key_env`` env var.
        api_key_env: Env var name for API key (default ``"API_KEY"``).
        max_tokens_key: ``"max_tokens"`` or ``"max_completion_tokens"``.
        default_headers: Optional HTTP headers sent with every request.
        cost_calculator: Optional cost callback. ``None`` = unknown cost.
        extra_kwargs: Extra kwargs for ``chat.completions.create``.
        provider_name: Human-readable name for ``repr()``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str = "API_KEY",
        max_tokens_key: str = "max_tokens",
        default_headers: dict[str, str] | None = None,
        cost_calculator: "CostCalculator | None" = None,
        extra_kwargs: dict[str, Any] | None = None,
        provider_name: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_tokens_key = max_tokens_key
        self._cost_calculator = cost_calculator
        self._extra_kwargs = extra_kwargs
        self._provider_name = provider_name or "OpenAICompatible"

        resolved_key = api_key or os.environ.get(api_key_env) or ""
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            default_headers=default_headers or None,
        )

    def __repr__(self) -> str:
        return f"{self._provider_name}Chat(base_url={self._base_url!r})"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        kwargs: dict[str, Any] = {
            "client": self._client,
            "messages": messages,
            "config": config,
            "tools": tools,
            "max_tokens_key": self._max_tokens_key,
            "extra_kwargs": self._extra_kwargs,
        }
        if self._cost_calculator is not None:
            kwargs["cost_calculator"] = self._cost_calculator

        async for event in stream_chat_completions(**kwargs):
            yield event
