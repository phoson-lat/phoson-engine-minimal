"""Shared human-readable labels for provider ids.

Single source of truth for the provider id → display-name mapping used by
every UI surface (setup wizard, provider picker, ...). Previously the same
table was duplicated in ``installer.py`` and ``provider_picker.py`` and had
already drifted (the picker carried ``grok``/``google``/``aws`` aliases the
wizard lacked).
"""

#: Provider id → display name. Aliases (e.g. ``grok`` → ``xai``) share the
#: canonical label.
PROVIDER_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
    "github": "GitHub Models",
    "nvidia": "NVIDIA",
    "xai": "Grok (X.AI)",
    "grok": "Grok (X.AI)",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "together": "Together AI",
    "perplexity": "Perplexity",
    "lmstudio": "LM Studio",
    "vllm": "vLLM",
    "azure": "Azure OpenAI",
    "gemini": "Google Gemini",
    "google": "Google Gemini",
    "mistral": "Mistral AI",
    "bedrock": "AWS Bedrock",
    "aws": "AWS Bedrock",
    "fireworks": "Fireworks AI",
    "cohere": "Cohere",
}


def provider_label(provider: str) -> str:
    """Display name for a provider id (the id itself when unknown)."""
    return PROVIDER_LABELS.get(provider, provider)


__all__ = ["PROVIDER_LABELS", "provider_label"]
