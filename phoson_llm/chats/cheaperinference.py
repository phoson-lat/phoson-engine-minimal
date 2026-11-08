"""Cheaper Inference adapter. Uses the ``CHEAPER_INFERENCE_API_KEY`` env var.

Cheaper Inference (https://www.cheaperinference.com) serves models from many
vendors (Claude, GPT, Gemini, DeepSeek, GLM, …) behind one OpenAI-compatible
Chat Completions endpoint, at discounted prices. Model ids are plain
(``claude-opus-4.6``, ``gpt-5.4``); ``GET /v1/models`` lists them.

Notes from the provider docs: keys start with ``ci_live_``; ``reasoning_effort``
is accepted on Chat Completions; chat streams carry no billing fields, so cost
is computed from the local price table (unknown models report 0).

Prompt caching (docs, "Prompt caching"):

- **Claude** models are only cached when the request carries
  ``cache_control`` markers — the gateway's default passthrough mode adds
  none. Claude requests therefore get the shared explicit-cache markers on
  the system prompt (tool definitions ride along with it in Anthropic's
  ``tools → system → messages`` prefix order) and on the last message, so the
  cached prefix advances with the conversation.
- Other vendors (OpenAI, DeepSeek, …) cache implicitly upstream; no markers
  are sent for them.
- Every request with a ``session_id`` sends ``x-ci-prompt-cache-session`` so
  the gateway routes a conversation consistently and the upstream cache stays
  warm (helps implicit caches too).

Cached reads come back in ``usage.prompt_tokens_details.cached_tokens``, which
the shared streaming loop already reads.
"""

from typing import Any

from phoson_llm.schemas import ModelConfig
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

CHEAPER_INFERENCE_BASE_URL = "https://api.cheaperinference.com/v1"
ENV_VAR = "CHEAPER_INFERENCE_API_KEY"

#: Routing-affinity header: one stable value per conversation.
CACHE_SESSION_HEADER = "x-ci-prompt-cache-session"


def supports_explicit_cache(model: str) -> bool:
    """Whether ``model`` needs explicit ``cache_control`` markers (Claude)."""
    name = model.strip().lower().removeprefix("anthropic/")
    return name.startswith("claude")


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

    def _cache_enabled(self, model: str) -> bool:
        return supports_explicit_cache(model)

    def _request_kwargs(self, config: ModelConfig) -> dict[str, Any] | None:
        base = super()._request_kwargs(config)
        if not config.session_id:
            return base
        kwargs = dict(base or {})
        headers = dict(kwargs.get("extra_headers") or {})
        headers[CACHE_SESSION_HEADER] = config.session_id
        kwargs["extra_headers"] = headers
        return kwargs
