"""NVIDIA NIM adapter. Uses the ``NVIDIA_API_KEY`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
ENV_VAR = "NVIDIA_API_KEY"


class NVIDIAChat(OpenAICompatibleChat):
    """Adapter for NVIDIA NIM (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or NVIDIA_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="NVIDIA",
        )
