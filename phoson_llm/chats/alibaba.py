"""Alibaba Cloud (DashScope / Model Studio) adapter.

Uses DashScope's OpenAI-compatible endpoint and the ``DASHSCOPE_API_KEY`` env
var. The default base URL is the international one; Mainland China accounts
should pass ``https://dashscope.aliyuncs.com/compatible-mode/v1``.

Prompt caching
--------------
Model Studio supports both an *implicit* cache (enabled automatically whenever
a request carries no cache marker) and an *explicit* cache, where the client
places ``"cache_control": {"type": "ephemeral"}`` on a content part and the
provider caches everything from the start of the ``messages`` array up to it.
Explicit caching delivers deterministic hits but, being mutually exclusive with
the implicit one, a request either opts in fully or not at all. This adapter
opts in for the models that document the marker: the shared OpenAI-compatible
loop tags the stable prefix (system prompt + tool definitions, which Model
Studio counts as part of the system prompt for caching) and the last message,
so the cached prefix advances with the conversation.

Only a subset of models support explicit caching; for any other model the
marker is omitted so the request stays on the implicit path rather than
risking a rejected request. See :func:`supports_explicit_cache` and the
"Explicit Cache Best Practices" guide on Model Studio.
"""

import re

from phoson_llm.pricing import calculate_cost
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

ALIBABA_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
ENV_VAR = "DASHSCOPE_API_KEY"


#: Base model ids documented as supporting Model Studio's *explicit* context
#: cache (Singapore / international deployment scope). Dated snapshots
#: (``-0902``, ``-2026-05-20``) and the ``-latest`` / ``-preview`` aliases of
#: these bases are accepted by :func:`supports_explicit_cache`.
_EXPLICIT_CACHE_MODELS = frozenset(
    {
        # Qwen Max
        "qwen3.8-max",
        "qwen3.7-max",
        "qwen3.6-max-preview",
        "qwen3-max",
        # Qwen open-source
        "qwen3.8-2.4t-a95b",
        "qwen3.8-27b",
        # Qwen Plus
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
        "qwen-plus",
        # Qwen Flash
        "qwen3.8-flash",
        "qwen3.7-flash",
        "qwen3.6-flash",
        "qwen3.5-flash",
        "qwen-flash",
        # Qwen Coder
        "qwen3-coder-plus",
        "qwen3-coder-flash",
        # Qwen VL
        "qwen3-vl-plus",
        "qwen3-vl-flash",
        # Third-party
        "deepseek-v3.2",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        "glm-5.1",
    }
)

#: Allowed suffix on an explicit-cache base id: a dated snapshot
#: (``2026-05-20`` / ``0902``) or the ``latest`` / ``preview`` alias.
_SNAPSHOT_SUFFIX = re.compile(r"(?:latest|preview|\d{4}(?:-\d{2}(?:-\d{2})?)?)\Z")


def supports_explicit_cache(model: str) -> bool:
    """Return whether ``model`` documents Model Studio's explicit cache.

    Accepts the bare model id (``qwen3.8-max``) and the provider-prefixed form
    used by the price table (``alibaba/qwen3.8-max``), plus dated snapshots and
    the ``-latest`` alias of a supported base. Models outside the list — e.g.
    ``qwen-turbo`` or ``qwq-plus`` — are left on the implicit-cache path.
    """
    mid = model.strip().lower()
    if mid.startswith("alibaba/"):
        mid = mid[len("alibaba/") :]
    if mid in _EXPLICIT_CACHE_MODELS:
        return True
    for base in _EXPLICIT_CACHE_MODELS:
        if mid.startswith(base + "-") and _SNAPSHOT_SUFFIX.fullmatch(
            mid[len(base) + 1 :]
        ):
            return True
    return False


def _alibaba_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int = 0,
) -> tuple[float, bool]:
    """Price a call against the Alibaba Cloud section of the price table."""
    return calculate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider="alibaba",
    )


class AlibabaChat(OpenAICompatibleChat):
    """Adapter for Alibaba Cloud Model Studio (OpenAI-compatible endpoint).

    Explicit prompt caching is applied automatically for the models that
    support it (see :func:`supports_explicit_cache`).
    """

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
            cost_calculator=_alibaba_cost,
        )

    def _cache_enabled(self, model: str) -> bool:
        return supports_explicit_cache(model)
