"""vLLM local inference adapter."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

VLLM_DEFAULT_BASE_URL = "http://localhost:8000/v1"
ENV_VAR = "VLLM_API_KEY"


class VLLMChat(OpenAICompatibleChat):
    """Adapter for vLLM local inference server.

    Args:
        base_url: vLLM endpoint (default ``http://localhost:8000/v1``).
        api_key: Optional API key if vLLM is deployed with auth.
    """

    def __init__(
        self,
        base_url: str = VLLM_DEFAULT_BASE_URL,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="VLLM",
        )
