"""DeepSeek adapter. Uses the ``DEEPSEEK_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
ENV_VAR = "DEEPSEEK_API_KEY"


class DeepSeekChat(OpenAICompatibleChat):
    """Adapter for DeepSeek (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or DEEPSEEK_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="DeepSeek",
        )
