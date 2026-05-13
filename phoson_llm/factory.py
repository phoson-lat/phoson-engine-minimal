"""Provider factory — maps provider names to chat adapter instances."""

from __future__ import annotations

from typing import Any

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openrouter import OpenRouterChat
from phoson_llm.exceptions import PhosonLLMError


def build_chat(
    provider: str,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> BaseLLMChat:
    """Build an LLM chat adapter by provider name.

    Args:
        provider: Provider name.
        api_key: API key (defaults to the provider's env var).
        base_url: Optional base URL override.
        **kwargs: Extra constructor arguments.

    Returns:
        An instance of the appropriate ``BaseLLMChat`` subclass.

    Raises:
        PhosonLLMError: If the provider name is unknown.
    """
    _PROVIDERS: dict[str, type[BaseLLMChat]] = {
        "openai": OpenAIChat,
        "anthropic": AnthropicChat,
        "ollama": OllamaChat,
        "openrouter": OpenRouterChat,
    }

    cls = _PROVIDERS.get(provider.lower())
    if cls is None:
        raise PhosonLLMError(
            f"Unknown provider: {provider!r}. "
            f"Available: {', '.join(sorted(_PROVIDERS))}"
        )

    init_kwargs: dict[str, Any] = {}
    if api_key is not None:
        init_kwargs["api_key"] = api_key
    if base_url is not None:
        init_kwargs["base_url"] = base_url
    init_kwargs.update(kwargs)

    return cls(**init_kwargs)
