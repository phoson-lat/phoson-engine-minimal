"""Groq adapter. Uses the ``GROQ_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
ENV_VAR = "GROQ_API_KEY"


class GroqChat(OpenAICompatibleChat):
    """Adapter for Groq (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            base_url=GROQ_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Groq",
        )
