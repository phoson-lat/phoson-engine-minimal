import warnings
from dataclasses import dataclass
from collections.abc import Callable

# Prices are stored per million tokens for readability; we divide by this
# constant when computing the cost of an actual call.
_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class PriceEntry:
    """
    Prices per million tokens (USD).

    ``cache_write`` / ``cache_read`` apply to providers with prompt caching.
    ``None`` (the default) means the provider publishes no separate rate, so
    those tokens are billed at the normal ``input`` rate; ``0.0`` means they
    are explicitly free (e.g. Gemini implicit-cache writes).
    """

    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """USD cost of a call; ``input_tokens`` is the net (uncached) input."""
        write = self.input if self.cache_write is None else self.cache_write
        read = self.input if self.cache_read is None else self.cache_read
        cost = (
            input_tokens * self.input
            + output_tokens * self.output
            + cache_write_tokens * write
            + cache_read_tokens * read
        ) / _TOKENS_PER_MILLION
        return round(cost, 8)


# ─── Price table (per million tokens, USD) ────────────────────────────────────
# Verified April 2026; current lineups re-verified Nov 2026 (see per-section
# notes). Update when providers change. Adapters whose provider reports the
# charged cost (OpenRouter, Perplexity) or publishes a live catalog (Cheaper
# Inference) do not use this table.
# Naming convention: provider/model, without version suffixes (see _ALIASES).

PRICES: dict[str, PriceEntry] = {
    # ── Anthropic ────────────────────────────────────────────────────────────
    # cache_write = 1.25x input, cache_read = 0.10x input
    "anthropic/claude-opus-4-7": PriceEntry(
        input=5.00, output=25.00, cache_write=6.25, cache_read=0.50
    ),
    "anthropic/claude-opus-4-6": PriceEntry(
        input=5.00, output=25.00, cache_write=6.25, cache_read=0.50
    ),
    "anthropic/claude-sonnet-4-6": PriceEntry(
        input=3.00, output=15.00, cache_write=3.75, cache_read=0.30
    ),
    "anthropic/claude-haiku-4-5": PriceEntry(
        input=1.00, output=5.00, cache_write=1.25, cache_read=0.10
    ),
    # Current lineup, verified Nov 2026 (platform.claude.com pricing; base tier,
    # 5-minute cache write). claude-haiku-5-5: prompts up to 100K tokens.
    "anthropic/claude-fable-5-1": PriceEntry(
        input=10, output=50, cache_write=12.5, cache_read=0.25
    ),
    "anthropic/claude-fable-5": PriceEntry(
        input=10, output=50, cache_write=12.5, cache_read=1.0
    ),
    "anthropic/claude-opus-5-5": PriceEntry(
        input=4, output=20, cache_write=5.0, cache_read=0.2
    ),
    "anthropic/claude-opus-5": PriceEntry(
        input=5, output=25, cache_write=6.25, cache_read=0.5
    ),
    "anthropic/claude-opus-4-8": PriceEntry(
        input=5, output=25, cache_write=6.25, cache_read=0.5
    ),
    "anthropic/claude-sonnet-5-5": PriceEntry(
        input=2, output=10, cache_write=2.5, cache_read=0.1
    ),
    "anthropic/claude-sonnet-5": PriceEntry(
        input=2, output=10, cache_write=2.5, cache_read=0.2
    ),
    "anthropic/claude-haiku-5-5": PriceEntry(
        input=0.1, output=0.5, cache_write=0.125, cache_read=0.01
    ),
    # ── OpenAI ───────────────────────────────────────────────────────────────
    # cache_read = 0.50x input (OpenAI cached input = 50% off)
    "openai/gpt-4o": PriceEntry(input=2.50, output=10.00, cache_read=1.25),
    "openai/gpt-4o-mini": PriceEntry(input=0.15, output=0.60, cache_read=0.075),
    "openai/o3": PriceEntry(input=2.00, output=8.00, cache_read=1.00),
    "openai/o4-mini": PriceEntry(input=1.10, output=4.40, cache_read=0.275),
    # Current lineup, verified Nov 2026 (developers.openai.com pricing; short
    # tier ≤272K input). OpenAI bills no separate cache write.
    "openai/gpt-6-astra": PriceEntry(input=10, output=50, cache_read=1.0),
    "openai/gpt-6.1-sol": PriceEntry(input=2, output=10, cache_read=0.1),
    "openai/gpt-6.1-terra": PriceEntry(input=1, output=5, cache_read=0.05),
    "openai/gpt-6-sol": PriceEntry(input=2, output=10, cache_read=0.2),
    "openai/gpt-6-luna": PriceEntry(input=0.1, output=0.5, cache_read=0.01),
    "openai/gpt-5.6-sol": PriceEntry(input=4, output=20, cache_read=0.4),
    "openai/gpt-5.6-terra": PriceEntry(input=2, output=12, cache_read=0.2),
    "openai/gpt-5.6-luna": PriceEntry(input=0.2, output=1.2, cache_read=0.02),
    "openai/gpt-5.5": PriceEntry(input=5, output=30, cache_read=0.5),
    "openai/gpt-5.5-pro": PriceEntry(input=30, output=180),
    "openai/gpt-5.4": PriceEntry(input=2.5, output=15, cache_read=0.25),
    "openai/gpt-5.4-pro": PriceEntry(input=30, output=180),
    "openai/gpt-5.4-mini": PriceEntry(input=0.75, output=4.5, cache_read=0.075),
    "openai/gpt-5.4-nano": PriceEntry(input=0.2, output=1.25, cache_read=0.02),
    "openai/gpt-5.3-codex": PriceEntry(input=1.75, output=14, cache_read=0.175),
    "openai/gpt-5.2": PriceEntry(input=1.75, output=14, cache_read=0.175),
    "openai/gpt-5.2-pro": PriceEntry(input=21, output=168),
    "openai/gpt-5.1": PriceEntry(input=1.25, output=10, cache_read=0.125),
    "openai/gpt-5": PriceEntry(input=1.25, output=10, cache_read=0.125),
    "openai/gpt-5-mini": PriceEntry(input=0.25, output=2, cache_read=0.025),
    "openai/gpt-5-nano": PriceEntry(input=0.05, output=0.4, cache_read=0.005),
    "openai/gpt-5-pro": PriceEntry(input=15, output=120),
    "openai/gpt-4.1": PriceEntry(input=2, output=8, cache_read=0.5),
    "openai/gpt-4.1-mini": PriceEntry(input=0.4, output=1.6, cache_read=0.1),
    "openai/o3-pro": PriceEntry(input=20, output=80),
    # ── Google Gemini ─────────────────────────────────────────────────────────
    # Google does not charge for cache_write, only cache_read (10% of input)
    "google/gemini-2.5-pro": PriceEntry(input=1.25, output=10.00, cache_read=0.125),
    "google/gemini-2.5-flash": PriceEntry(input=0.30, output=2.50, cache_read=0.03),
    "google/gemini-2.5-flash-lite": PriceEntry(
        input=0.10, output=0.40, cache_read=0.01
    ),
    "google/gemini-2.0-flash": PriceEntry(input=0.10, output=0.40, cache_read=0.01),
    "google/gemini-3-flash": PriceEntry(
        input=0.50, output=3.00, cache_write=0.0833333333, cache_read=0.05
    ),
    # Current lineup, verified Nov 2026 (aistudio.google.com pricing; paid tier,
    # text, base context tier; cache storage per hour is not modelled).
    # gemini-4-argon rises to 4 / 0.20 / 20 on 2026-12-01.
    "google/gemini-4-argon": PriceEntry(
        input=2, output=10, cache_write=0.0, cache_read=0.1
    ),
    "google/gemini-3.8-flash": PriceEntry(
        input=0.75, output=3.75, cache_write=0.0, cache_read=0.075
    ),
    "google/gemini-3.6-flash": PriceEntry(
        input=0.75, output=3.75, cache_write=0.0, cache_read=0.075
    ),
    "google/gemini-3.5-flash-lite": PriceEntry(
        input=0.3, output=2.5, cache_write=0.0, cache_read=0.03
    ),
    "google/gemini-3.1-pro-preview": PriceEntry(
        input=2, output=12, cache_write=0.0, cache_read=0.2
    ),
    "google/gemini-3.1-flash-lite": PriceEntry(
        input=0.25, output=1.5, cache_write=0.0, cache_read=0.025
    ),
    # ── xAI (Grok) ────────────────────────────────────────────────────────────
    "xai/grok-3": PriceEntry(input=3.00, output=15.00),
    "xai/grok-3-mini": PriceEntry(input=0.30, output=0.60),
    # Verified Nov 2026 (docs.x.ai/docs/models; prompts under 200K).
    "xai/grok-4.7": PriceEntry(input=2, output=6, cache_read=0.5),
    "xai/grok-4.6": PriceEntry(input=2, output=6, cache_read=0.5),
    "xai/grok-4.5": PriceEntry(input=2, output=6, cache_read=0.3),
    "xai/grok-4.3": PriceEntry(input=1.25, output=2.5, cache_read=0.2),
    "xai/grok-4.20-0309-reasoning": PriceEntry(input=1.25, output=2.5, cache_read=0.2),
    "xai/grok-4.20-0309-non-reasoning": PriceEntry(
        input=1.25, output=2.5, cache_read=0.2
    ),
    "xai/grok-4.20-multi-agent-0309": PriceEntry(
        input=1.25, output=2.5, cache_read=0.2
    ),
    "xai/grok-build-0.1": PriceEntry(input=1, output=2, cache_read=0.2),
    # ── NVIDIA ─────────────────────────────────────────────────────────────────
    "nvidia/llama-3.1-nemotron": PriceEntry(input=0.10, output=0.40),
    # ── DeepSeek ──────────────────────────────────────────────────────────────
    "deepseek/deepseek-chat": PriceEntry(input=0.27, output=1.10),
    "deepseek/deepseek-reasoner": PriceEntry(input=0.55, output=2.19),
    # ── Alibaba Cloud (DashScope / Model Studio) ───────────────────────────────
    # International (Singapore) pay-as-you-go list prices; the adapter's default
    # base URL is the international endpoint. Tiered models use the first tier
    # (0–32K/256K context). Verified Oct 2026.
    # Explicit cache: creation = 1.25x input, read = 0.10x input (context cache).
    # Exception: qwen3.8-max / qwen3.8-flash (and qwen3.8-2.4t-a95b) do NOT bill
    # the hit at 10%; their true explicit-cache rates from the model pages
    # (Singapore / international) are used below.
    "alibaba/qwen3.8-max": PriceEntry(
        input=2.00, output=6.00, cache_write=2.50, cache_read=0.17
    ),
    "alibaba/qwen3.7-max": PriceEntry(
        input=2.50, output=7.50, cache_write=3.125, cache_read=0.25
    ),
    "alibaba/qwen3-max": PriceEntry(
        input=1.20, output=6.00, cache_write=1.50, cache_read=0.12
    ),
    "alibaba/qwen-max": PriceEntry(
        input=1.60, output=6.40, cache_write=2.00, cache_read=0.16
    ),
    "alibaba/qwen3.7-plus": PriceEntry(
        input=0.40, output=1.60, cache_write=0.50, cache_read=0.04
    ),
    "alibaba/qwen3.5-plus": PriceEntry(
        input=0.40, output=2.40, cache_write=0.50, cache_read=0.04
    ),
    "alibaba/qwen-plus": PriceEntry(
        input=0.40, output=1.20, cache_write=0.50, cache_read=0.04
    ),
    "alibaba/qwen3.8-flash": PriceEntry(
        input=0.15, output=0.47, cache_write=0.20, cache_read=0.016
    ),
    "alibaba/qwen3.7-flash": PriceEntry(
        input=0.030, output=0.130, cache_write=0.0375, cache_read=0.003
    ),
    "alibaba/qwen-flash": PriceEntry(
        input=0.05, output=0.40, cache_write=0.0625, cache_read=0.005
    ),
    "alibaba/qwen-turbo": PriceEntry(
        input=0.05, output=0.20, cache_write=0.0625, cache_read=0.005
    ),
    "alibaba/qwq-plus": PriceEntry(
        input=0.80, output=2.40, cache_write=1.00, cache_read=0.08
    ),
    # ── Groq ──────────────────────────────────────────────────────────────────
    "groq/llama-3.3-70b": PriceEntry(input=0.59, output=0.79),
    # Verified Nov 2026 (console.groq.com/docs/models).
    "groq/openai/gpt-oss-120b": PriceEntry(input=0.15, output=0.6),
    "groq/openai/gpt-oss-20b": PriceEntry(input=0.075, output=0.3),
    "groq/qwen/qwen3.8-27b": PriceEntry(input=0.8, output=4.0),
    # ── Together AI ───────────────────────────────────────────────────────────
    "together/llama-3.3-70b": PriceEntry(input=0.72, output=0.72),
    # ── Perplexity ────────────────────────────────────────────────────────────
    "perplexity/sonar-pro": PriceEntry(input=1.00, output=5.00),
    "perplexity/sonar": PriceEntry(input=0.20, output=1.00),
    # ── Mistral ───────────────────────────────────────────────────────────────
    "mistral/mistral-large-latest": PriceEntry(input=2.00, output=6.00),
    "mistral/mistral-small-latest": PriceEntry(input=0.50, output=1.50),
    "mistral/codestral-latest": PriceEntry(input=1.00, output=3.00),
    # ── AWS Bedrock ───────────────────────────────────────────────────────────
    "bedrock/claude-sonnet-4-6": PriceEntry(input=3.00, output=15.00),
    "bedrock/claude-haiku-4-5": PriceEntry(input=1.00, output=5.00),
    # ── Fireworks ─────────────────────────────────────────────────────────────
    "fireworks/llama-3.3-70b": PriceEntry(input=0.50, output=0.50),
    # ── Cohere ────────────────────────────────────────────────────────────────
    "cohere/command-r-plus": PriceEntry(input=2.50, output=10.00),
    "cohere/command-r": PriceEntry(input=0.50, output=1.50),
}

# Aliases — the SDK may send strings with version suffixes
_ALIASES: dict[str, str] = {
    "anthropic/claude-sonnet-4-6-20250514": "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5-20251001": "anthropic/claude-haiku-4-5",
    "anthropic/claude-opus-4-6-20250514": "anthropic/claude-opus-4-6",
    "anthropic/claude-opus-4-7-20250514": "anthropic/claude-opus-4-7",
    "openai/gpt-4o-2024-11-20": "openai/gpt-4o",
    "openai/gpt-4o-mini-2024-07-18": "openai/gpt-4o-mini",
    "google/gemini-2.5-pro-preview": "google/gemini-2.5-pro",
    "google/gemini-2.5-flash-preview": "google/gemini-2.5-flash",
    "google/gemini-3-flash-preview": "google/gemini-3-flash",
    "alibaba/qwen-max-latest": "alibaba/qwen-max",
    "alibaba/qwen-plus-latest": "alibaba/qwen-plus",
    "alibaba/qwen3.8-max-0902": "alibaba/qwen3.8-max",
    "alibaba/qwen3.7-max-2026-06-08": "alibaba/qwen3.7-max",
    "alibaba/qwen3.7-plus-2026-05-26": "alibaba/qwen3.7-plus",
    "alibaba/qwen3-max-2026-01-23": "alibaba/qwen3-max",
    "alibaba/qwen-flash-2025-07-28": "alibaba/qwen-flash",
    "alibaba/qwen3.7-flash-2026-07-15": "alibaba/qwen3.7-flash",
}


class UnknownModelWarning(UserWarning):
    """Emitted when :func:`calculate_cost` encounters a model not in the price table.

    Silence this for providers where unknown costs are expected (e.g. Ollama,
    custom OpenRouter routes)::

        import warnings
        from phoson_llm.pricing import UnknownModelWarning
        warnings.filterwarnings("ignore", category=UnknownModelWarning)
    """


def _resolve(model: str, provider: str | None = None) -> PriceEntry | None:
    """Resolves the model to the corresponding PriceEntry, with support for aliases.

    Lookups are case-insensitive. With ``provider`` the ``provider/model`` key
    is tried as well, also for model ids that contain a slash themselves
    (Together's ``meta-llama/Llama-3.3-70B-Instruct-Turbo``).
    """
    key = model.strip().lower()
    candidates = [key]
    if provider:
        candidates.append(f"{provider.lower()}/{key}")
    for candidate in candidates:
        entry = PRICES.get(_ALIASES.get(candidate, candidate))
        if entry is not None:
            return entry
    return None


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    provider: str | None = None,
) -> tuple[float, bool]:
    """
    Calculates the real cost in USD for an LLM call.

    Args:
        model (str): Name of the model.
        input_tokens (int): Input tokens.
        output_tokens (int): Output tokens.
        cache_write_tokens (int): Cache write tokens.
        cache_read_tokens (int): Cache read tokens.
        provider (str | None): Optional provider.

    Returns:
        Tuple[float, bool]: (cost_usd, cost_known).
        cost_known=False when the model is not in the table
        (e.g. local Ollama models).
    """
    entry = _resolve(model, provider=provider)

    if entry is None:
        warnings.warn(
            f"No price entry for model {model!r}; cost will be reported as 0. "
            "Add it to phoson_llm.pricing.PRICES.",
            UnknownModelWarning,
            stacklevel=2,
        )
        return 0.0, False

    return (
        entry.cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
        ),
        True,
    )


def price_table_calculator(provider: str) -> Callable[..., tuple[float, bool]]:
    """A ``CostCalculator`` that prices calls from :data:`PRICES`.

    Used by the OpenAI-compatible adapters whose provider reports no cost of
    its own. Models missing from the table report ``(0.0, False)``.
    """

    def _cost(
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> tuple[float, bool]:
        return calculate_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            provider=provider,
        )

    return _cost
