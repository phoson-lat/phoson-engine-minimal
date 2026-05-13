"""Perplexity AI adapter. Uses the ``PERPLEXITY_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
ENV_VAR = "PERPLEXITY_API_KEY"


class PerplexityChat(OpenAICompatibleChat):
    """Adapter for Perplexity AI (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            base_url=PERPLEXITY_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Perplexity",
        )
