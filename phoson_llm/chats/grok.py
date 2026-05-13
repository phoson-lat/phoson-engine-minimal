"""xAI Grok adapter. Uses the ``XAI_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

GROK_BASE_URL = "https://api.x.ai/v1"
ENV_VAR = "XAI_API_KEY"


class GrokChat(OpenAICompatibleChat):
    """Adapter for xAI Grok (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            base_url=GROK_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Grok",
        )
