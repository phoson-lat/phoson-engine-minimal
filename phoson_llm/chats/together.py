"""Together AI adapter. Uses the ``TOGETHER_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

TOGETHER_BASE_URL = "https://api.together.xyz/v1"
ENV_VAR = "TOGETHER_API_KEY"


class TogetherChat(OpenAICompatibleChat):
    """Adapter for Together AI (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or TOGETHER_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Together",
        )
