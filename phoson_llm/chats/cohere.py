"""Cohere adapter. Uses the ``COHERE_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

COHERE_BASE_URL = "https://api.cohere.com/v1"
ENV_VAR = "COHERE_API_KEY"


class CohereChat(OpenAICompatibleChat):
    """Adapter for Cohere (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            base_url=COHERE_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Cohere",
        )
