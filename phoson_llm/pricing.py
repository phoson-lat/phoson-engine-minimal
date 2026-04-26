from dataclasses import dataclass


@dataclass(frozen=True)
class PriceEntry:
    """
    Precios por millón de tokens (USD).
    cache_write y cache_read solo aplican a providers que soporten prompt caching.
    """

    input: float
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0


def _per_million(n: float) -> float:
    return n / 1_000_000


# ─── Tabla de precios (por millón de tokens, USD) ─────────────────────────────
# Verificados abril 2026. Actualizar cuando cambien los providers.
# Convención de nombres: provider/modelo, sin sufijos de versión (ver _ALIASES).

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
    # Google no cobra cache_write, solo cache_read (10% del input)
    "google/gemini-2.5-pro": PriceEntry(input=1.25, output=10.00, cache_read=0.125),
    "google/gemini-2.5-flash": PriceEntry(input=0.30, output=2.50, cache_read=0.03),
    "google/gemini-2.5-flash-lite": PriceEntry(
        input=0.10, output=0.40, cache_read=0.01
    ),
    "google/gemini-2.0-flash": PriceEntry(input=0.10, output=0.40, cache_read=0.01),
}

# Aliases — el SDK puede mandar strings con sufijos de versión
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


def _resolve(model: str, provider: str | None = None) -> PriceEntry | None:
    """Resuelve el modelo al PriceEntry correspondiente, con alias."""
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
    Calcula el costo real en USD para una LLM call.

    Retorna (cost_usd, cost_known).
    cost_known=False cuando el modelo no está en la tabla
    (e.g. modelos locales de Ollama).
    """
    entry = _resolve(model, provider=provider)

    if entry is None:
        return 0.0, False

    cost = (
        input_tokens * _per_million(entry.input)
        + output_tokens * _per_million(entry.output)
        + cache_write_tokens * _per_million(entry.cache_write)
        + cache_read_tokens * _per_million(entry.cache_read)
    )

    return round(cost, 8), True
