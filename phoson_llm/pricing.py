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
    "claude-sonnet-4-6-20250514": "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-opus-4-6-20250514": "claude-opus-4-6",
    "claude-opus-4-7-20250514": "claude-opus-4-7",
    "gpt-4o-2024-11-20": "gpt-4o",
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    "gemini-2.5-pro-preview": "gemini-2.5-pro",
    "gemini-2.5-flash-preview": "gemini-2.5-flash",
}


def _resolve(model: str) -> PriceEntry | None:
    """Resuelve el modelo al PriceEntry correspondiente, con alias."""
    key = _ALIASES.get(model, model)
    return PRICES.get(key)


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> tuple[float, bool]:
    """
    Calcula el costo real en USD para una LLM call.

    Retorna (cost_usd, cost_known).
    cost_known=False cuando el modelo no está en la tabla
    (e.g. modelos locales de Ollama).
    """
    entry = _resolve(model)

    if entry is None:
        return 0.0, False

    cost = (
        input_tokens * _per_million(entry.input)
        + output_tokens * _per_million(entry.output)
        + cache_write_tokens * _per_million(entry.cache_write)
        + cache_read_tokens * _per_million(entry.cache_read)
    )

    return round(cost, 8), True
