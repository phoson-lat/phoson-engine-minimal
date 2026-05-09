import warnings
from dataclasses import dataclass

# Prices are stored per million tokens for readability; we divide by this
# constant when computing the cost of an actual call.
_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class PriceEntry:
    """
    Prices per million tokens (USD).
    cache_write and cache_read only apply to providers that support prompt caching.
    """

    input: float
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0


# ─── Price table (per million tokens, USD) ────────────────────────────────────
# Verified April 2026. Update when providers change.
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
    # ── OpenAI ───────────────────────────────────────────────────────────────
    # cache_read = 0.50x input (OpenAI cached input = 50% off)
    "openai/gpt-4o": PriceEntry(input=2.50, output=10.00, cache_read=1.25),
    "openai/gpt-4o-mini": PriceEntry(input=0.15, output=0.60, cache_read=0.075),
    "openai/o3": PriceEntry(input=2.00, output=8.00, cache_read=1.00),
    "openai/o4-mini": PriceEntry(input=1.10, output=4.40, cache_read=0.275),
    # ── Google Gemini ─────────────────────────────────────────────────────────
    # Google does not charge for cache_write, only cache_read (10% of input)
    "google/gemini-2.5-pro": PriceEntry(input=1.25, output=10.00, cache_read=0.125),
    "google/gemini-2.5-flash": PriceEntry(input=0.30, output=2.50, cache_read=0.03),
    "google/gemini-2.5-flash-lite": PriceEntry(
        input=0.10, output=0.40, cache_read=0.01
    ),
    "google/gemini-2.0-flash": PriceEntry(input=0.10, output=0.40, cache_read=0.01),
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
    """Resolves the model to the corresponding PriceEntry, with support for aliases."""
    key = _ALIASES.get(model, model)
    entry = PRICES.get(key)
    if entry is not None:
        return entry

    if provider and "/" not in key:
        prefixed = f"{provider}/{key}"
        prefixed = _ALIASES.get(prefixed, prefixed)
        return PRICES.get(prefixed)

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
            "Add it to phoson_llm.pricing.PRICES or suppress with "
            "warnings.filterwarnings('ignore', category=UnknownModelWarning).",
            UnknownModelWarning,
            stacklevel=2,
        )
        return 0.0, False

    cost = (
        input_tokens * entry.input
        + output_tokens * entry.output
        + cache_write_tokens * entry.cache_write
        + cache_read_tokens * entry.cache_read
    ) / _TOKENS_PER_MILLION

    return round(cost, 8), True
