"""Provider factory — maps provider names to chat adapter instances.

Adapters are imported lazily, per provider, inside :func:`build_chat`. Each
adapter module pulls in a heavy vendor SDK (``openai``, ``anthropic``, …), so
importing them all at module import time cost ~1 s of startup and loaded SDKs
the user may never need. Now only the selected provider's adapter is imported,
and only when it is actually constructed.
"""

from typing import Any
from importlib import import_module

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.exceptions import PhosonLLMError

#: provider name → (adapter submodule, class name).
#: Kept as strings so no adapter module is imported until ``build_chat``.
_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": (".chats.openai", "OpenAIChat"),
    "anthropic": (".chats.anthropic", "AnthropicChat"),
    "ollama": (".chats.ollama", "OllamaChat"),
    "openrouter": (".chats.openrouter", "OpenRouterChat"),
    "github": (".chats.github_models", "GitHubModelsChat"),
    "nvidia": (".chats.nvidia", "NVIDIAChat"),
    "xai": (".chats.grok", "GrokChat"),
    "grok": (".chats.grok", "GrokChat"),
    "groq": (".chats.groq", "GroqChat"),
    "deepseek": (".chats.deepseek", "DeepSeekChat"),
    "together": (".chats.together", "TogetherChat"),
    "perplexity": (".chats.perplexity", "PerplexityChat"),
    "lmstudio": (".chats.lmstudio", "LMStudioChat"),
    "vllm": (".chats.vllm", "VLLMChat"),
    "azure": (".chats.azure", "AzureChat"),
    "gemini": (".chats.gemini", "GeminiChat"),
    "google": (".chats.gemini", "GeminiChat"),
    "mistral": (".chats.mistral", "MistralChat"),
    "bedrock": (".chats.bedrock", "BedrockChat"),
    "aws": (".chats.bedrock", "BedrockChat"),
    "fireworks": (".chats.fireworks", "FireworksChat"),
    "cohere": (".chats.cohere", "CohereChat"),
    "omniroute": (".chats.omniroute", "OmniRouteChat"),
}


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
    entry = _PROVIDERS.get(provider.lower())
    if entry is None:
        raise PhosonLLMError(
            f"Unknown provider: {provider!r}. "
            f"Available: {', '.join(sorted(_PROVIDERS))}"
        )
    module_name, class_name = entry
    # Lazy import: only the selected provider's (heavy) adapter is loaded.
    cls = getattr(import_module(module_name, __package__), class_name)

    init_kwargs: dict[str, Any] = {}
    if api_key is not None:
        init_kwargs["api_key"] = api_key
    if base_url is not None:
        init_kwargs["base_url"] = base_url
    init_kwargs.update(kwargs)

    return cls(**init_kwargs)
