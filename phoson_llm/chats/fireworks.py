"""Fireworks AI adapter. Uses the ``FIREWORKS_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
ENV_VAR = "FIREWORKS_API_KEY"


class FireworksChat(OpenAICompatibleChat):
    """Adapter for Fireworks AI (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or FIREWORKS_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Fireworks",
        )
