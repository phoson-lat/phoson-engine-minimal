"""Alibaba Cloud (DashScope / Model Studio) adapter.

Uses DashScope's OpenAI-compatible endpoint and the ``DASHSCOPE_API_KEY`` env
var. The default base URL is the international one; Mainland China accounts
should pass ``https://dashscope.aliyuncs.com/compatible-mode/v1``.
"""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

ALIBABA_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
ENV_VAR = "DASHSCOPE_API_KEY"


class AlibabaChat(OpenAICompatibleChat):
    """Adapter for Alibaba Cloud Model Studio (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or ALIBABA_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Alibaba",
        )
