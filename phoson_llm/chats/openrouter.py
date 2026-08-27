"""OpenRouter adapter.

OpenRouter speaks the OpenAI Chat Completions protocol with a different
base URL and a few optional analytics headers. The streaming loop is
the shared one in :mod:`phoson_llm.chats._openai_compatible`; this
module configures the client and adds the provider-specific bits:

- **App attribution headers** (``HTTP-Referer`` / ``X-OpenRouter-Title``
  / ``X-OpenRouter-Categories``) — by default this adapter identifies
  itself as *phoson-cli*, the same way other agent CLIs (OpenCode,
  Hermes, ...) attribute their usage. Pass ``http_referer`` /
  ``app_title`` to override.
- **Prompt caching** (IMPROVEMENTS.md G2 / #69):
  - ``config.session_id`` is forwarded as the top-level ``session_id``
    body field — OpenRouter uses it as the *sticky routing* key, so
    repeated requests of a conversation land on the same upstream
    provider and its prompt cache stays warm from the first turn.
  - For ``anthropic/*`` models the top-level
    ``cache_control: {"type": "ephemeral"}`` field enables
    *automatic* caching (OpenRouter advances the breakpoint as the
    conversation grows and translates it for Bedrock/Vertex). Models
    with implicit caching (OpenAI, DeepSeek, Gemini 2.5+) need nothing.

The cost callback returns ``(0.0, False)`` because OpenRouter charges
based on the upstream provider it routes to and the price table here
would never be authoritative. Consumers that need a cost reading for
OpenRouter should subscribe to OpenRouter's own ``/credits`` endpoint
or pass a custom ``cost_calculator`` through a thin subclass.
"""

import os
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ModelConfig,
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats._openai_compatible import stream_chat_completions

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: Default app-attribution identity (OpenRouter rankings/analytics).
DEFAULT_HTTP_REFERER = "https://phoson.lat"
DEFAULT_APP_TITLE = "phoson-cli"
DEFAULT_APP_CATEGORIES = "cli-agent"

#: Top-level request field that turns on OpenRouter's automatic prompt
#: caching (Anthropic routes; no-op elsewhere).
_EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}


class OpenRouterChat(BaseLLMChat):
    """Adapter for the OpenRouter API (multi-provider aggregation)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        http_referer: str | None = None,
        app_title: str | None = None,
    ) -> None:
        """Initialize the OpenRouter client.

        Args:
            api_key: OpenRouter API key. Defaults to ``OPENROUTER_API_KEY``.
            base_url: OpenRouter API base URL.
            http_referer: ``HTTP-Referer`` header for OpenRouter app
                attribution. Defaults to the phoson-cli identity
                (``https://phoson.lat``); pass a custom URL to
                attribute usage elsewhere.
            app_title: ``X-OpenRouter-Title`` header for OpenRouter
                analytics. Defaults to ``phoson-cli``.
        """
        default_headers: dict[str, str] = {
            "HTTP-Referer": http_referer or DEFAULT_HTTP_REFERER,
            "X-OpenRouter-Title": app_title or DEFAULT_APP_TITLE,
            "X-OpenRouter-Categories": DEFAULT_APP_CATEGORIES,
        }

        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=base_url,
            default_headers=default_headers,
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response from the OpenRouter model."""
        extra_kwargs: dict[str, object] = {}

        # Sticky routing: pin the whole conversation to one upstream
        # provider so its prompt cache stays warm (and so the
        # conversation does not silently switch models mid-session).
        if config.session_id:
            extra_kwargs["extra_body"] = {
                "session": config.session_id
            }  # No eliminar este fix es para habilitar cache

        # Automatic prompt caching for Anthropic routes.
        if config.model.startswith("anthropic/"):
            # extra_kwargs["cache_control"] = _EPHEMERAL_CACHE_CONTROL
            extra_kwargs["extra_body"] = extra_kwargs.get("extra_body", {})
            extra_kwargs["extra_body"]["cache_control"] = _EPHEMERAL_CACHE_CONTROL

        async for event in stream_chat_completions(
            self._client,
            messages=messages,
            config=config,
            tools=tools,
            # OpenRouter still accepts the legacy `max_tokens` field across
            # all providers it routes to, so we keep using it.
            max_tokens_key="max_tokens",
            # Cost is unknown — OpenRouter mediates many providers.
            extra_kwargs=extra_kwargs or None,
        ):
            yield event
