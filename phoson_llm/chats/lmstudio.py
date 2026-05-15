"""LM Studio local inference adapter."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

LMSTUDIO_DEFAULT_BASE_URL = "http://localhost:1234/v1"


class LMStudioChat(OpenAICompatibleChat):
    """Adapter for LM Studio local inference server.

    Args:
        base_url: LM Studio endpoint (default ``http://localhost:1234/v1``).
    """

    def __init__(self, base_url: str = LMSTUDIO_DEFAULT_BASE_URL) -> None:
        super().__init__(
            base_url=base_url,
            api_key="not-needed",
            provider_name="LMStudio",
        )
