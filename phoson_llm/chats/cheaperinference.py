"""Cheaper Inference adapter. Uses the ``CHEAPER_INFERENCE_API_KEY`` env var.

Cheaper Inference (https://www.cheaperinference.com) serves models from many
vendors (Claude, GPT, Gemini, DeepSeek, GLM, …) behind one OpenAI-compatible
Chat Completions endpoint, at discounted prices. Model ids are plain
(``claude-opus-4.6``, ``gpt-5.4``); ``GET /v1/models`` lists them.

Notes from the provider docs: keys start with ``ci_live_``; ``reasoning_effort``
is accepted on Chat Completions.

Cost: chat streams carry no billing fields, so the adapter prices each call
from the gateway's live catalog, ``GET /public/models`` (no key needed; USD per
million tokens as decimal strings, including cache-read/-write rates and a
long-context tier). The catalog is fetched lazily on the first request and
cached for :data:`CATALOG_TTL_S`; when it cannot be fetched, or the model is
not in it, the cost is reported as unknown rather than guessed.

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

import time
import asyncio
import logging
from typing import Any
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from collections.abc import AsyncIterator

import httpx

from phoson_llm.pricing import PriceEntry
from phoson_llm.schemas import Message, LLMEvent, ModelConfig, ToolDefinition
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

logger = logging.getLogger(__name__)

CHEAPER_INFERENCE_BASE_URL = "https://api.cheaperinference.com/v1"
ENV_VAR = "CHEAPER_INFERENCE_API_KEY"

#: Routing-affinity header: one stable value per conversation.
CACHE_SESSION_HEADER = "x-ci-prompt-cache-session"

#: Keyless live price catalog.
CATALOG_URL = "https://api.cheaperinference.com/public/models"
#: How long a fetched catalog is reused (prices change rarely).
CATALOG_TTL_S = 6 * 3600
#: After a failed fetch, wait this long before trying again.
CATALOG_RETRY_S = 600
_CATALOG_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class ModelPrice:
    """A catalog price: base rates plus an optional long-context tier."""

    base: PriceEntry
    #: Whole-request rates once the gross prompt exceeds ``threshold``.
    above: PriceEntry | None = None
    threshold: int | None = None

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> float:
        prompt = input_tokens + cache_read_tokens + cache_write_tokens
        entry = self.base
        if self.above is not None and self.threshold and prompt > self.threshold:
            entry = self.above
        return entry.cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )


def _rate(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    return float(rate) if rate.is_finite() and rate >= 0 else None


def _entry(
    input_rate: object, output_rate: object, read: object, write: object
) -> PriceEntry | None:
    base_in, base_out = _rate(input_rate), _rate(output_rate)
    if base_in is None or base_out is None:
        return None
    cache_read = _rate(read)
    return PriceEntry(
        input=base_in,
        output=base_out,
        # Provider rule: a missing read rate is 10% of input; a missing
        # write rate is the normal input rate (PriceEntry's ``None``).
        cache_read=base_in * 0.1 if cache_read is None else cache_read,
        cache_write=_rate(write),
    )


def parse_catalog(payload: object) -> dict[str, ModelPrice]:
    """Map every model id and alias (lower-cased) to its price.

    Non-text models and rows without usable token rates are skipped.
    """
    prices: dict[str, ModelPrice] = {}
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return prices
    for model in models:
        if not isinstance(model, dict) or model.get("model_type", "text") != "text":
            continue
        base = _entry(
            model.get("input_per_million"),
            model.get("output_per_million"),
            model.get("cache_read_per_million"),
            model.get("cache_write_per_million"),
        )
        if base is None:
            continue
        above: PriceEntry | None = None
        threshold: int | None = None
        tier = model.get("above_threshold")
        # Only the unconditional tier is applied: one that depends on the
        # serving supplier's own premium cannot be known client-side.
        if (
            isinstance(tier, dict)
            and tier.get("applies_when") == "prompt_tokens_above_threshold"
        ):
            above = _entry(
                tier.get("input_per_million"),
                tier.get("output_per_million"),
                tier.get("cache_read_input_per_million"),
                tier.get("cache_write_input_per_million"),
            )
            raw = tier.get("input_token_price_threshold")
            threshold = raw if isinstance(raw, int) and raw > 0 else None
        price = ModelPrice(
            base=base, above=above if threshold else None, threshold=threshold
        )
        names = [model.get("id"), *(model.get("aliases") or [])]
        for name in names:
            if isinstance(name, str) and name:
                prices[name.strip().lower()] = price
    return prices


class _Catalog:
    """Process-wide cache of the live catalog (shared by all adapters)."""

    def __init__(self) -> None:
        self.prices: dict[str, ModelPrice] = {}
        self.fetched_at = 0.0
        self.failed_at = 0.0
        self.lock = asyncio.Lock()

    def fresh(self, now: float) -> bool:
        if self.prices and now - self.fetched_at < CATALOG_TTL_S:
            return True
        return bool(self.failed_at) and now - self.failed_at < CATALOG_RETRY_S

    async def refresh(self, url: str) -> None:
        if self.fresh(time.monotonic()):
            return
        async with self.lock:
            now = time.monotonic()
            if self.fresh(now):
                return
            try:
                async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT_S) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    prices = parse_catalog(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("Cheaper Inference price catalog unavailable: %s", exc)
                self.failed_at = now
                return
            if prices:
                self.prices, self.fetched_at, self.failed_at = prices, now, 0.0
            else:
                self.failed_at = now


_CATALOG = _Catalog()


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
            cost_calculator=self._catalog_cost,
        )
        self._catalog = _CATALOG
        self._catalog_url = CATALOG_URL

    def _catalog_cost(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> tuple[float, bool]:
        price = self._catalog.prices.get(model.strip().lower())
        if price is None:
            return 0.0, False
        return (
            price.cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            True,
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        # Never let pricing delay or break a turn beyond the fetch timeout;
        # a failure just leaves the cost unknown.
        await self._catalog.refresh(self._catalog_url)
        async for event in super().stream(messages, config, tools):
            yield event

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
