"""Cheaper Inference adapter. Uses the ``CHEAPER_INFERENCE_API_KEY`` env var.

Cheaper Inference (https://www.cheaperinference.com) serves models from many
vendors (Claude, GPT, Gemini, DeepSeek, GLM, …) behind one OpenAI-compatible
Chat Completions endpoint, at discounted prices. Model ids are plain
(``claude-opus-4.6``, ``gpt-5.4``); ``GET /v1/models`` lists them.

Notes from the provider docs: keys start with ``ci_live_``; ``reasoning_effort``
is accepted on Chat Completions; chat streams carry no billing fields, so cost
is computed from the local price table (unknown models report 0).
"""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

CHEAPER_INFERENCE_BASE_URL = "https://api.cheaperinference.com/v1"
ENV_VAR = "CHEAPER_INFERENCE_API_KEY"


class CheaperInferenceChat(OpenAICompatibleChat):
    """Adapter for Cheaper Inference (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or CHEAPER_INFERENCE_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="Cheaper Inference",
        )
