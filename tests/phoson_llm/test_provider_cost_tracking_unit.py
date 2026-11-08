"""Cost tracking for every API provider.

- Price-table adapters (DeepSeek, xAI, Groq, Together, Fireworks, Perplexity,
  Cohere, NVIDIA, Azure) are wired to :data:`phoson_llm.pricing.PRICES`.
- Perplexity's ``usage.cost.total_cost`` breakdown is read as the charged cost.
- Cheaper Inference prices calls from its live ``/public/models`` catalog.
"""

from types import SimpleNamespace

import httpx
import pytest

from phoson_llm.chats import cheaperinference as ci
from phoson_llm.pricing import (
    PriceEntry,
    UnknownModelWarning,
    calculate_cost,
    price_table_calculator,
)
from phoson_llm.chats._openai_compatible import _extract_provider_cost

# ─── Price table ─────────────────────────────────────────────────────────────


def test_missing_cache_rate_falls_back_to_input_rate() -> None:
    entry = PriceEntry(input=2.0, output=4.0)
    cost = entry.cost(
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost == pytest.approx(4.0)


def test_explicit_zero_cache_rate_is_free() -> None:
    entry = PriceEntry(input=2.0, output=4.0, cache_write=0.0, cache_read=0.0)
    assert entry.cost(
        input_tokens=0, output_tokens=0, cache_read_tokens=10, cache_write_tokens=10
    ) == pytest.approx(0.0)


def test_slashed_model_ids_resolve_with_provider_prefix() -> None:
    # Groq serves ``openai/gpt-oss-120b``: the key is ``groq/openai/gpt-oss-120b``.
    cost, known = calculate_cost(
        "openai/gpt-oss-120b",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        provider="groq",
    )
    assert known is True
    assert cost == pytest.approx(0.15 + 0.60)


def test_lookup_is_case_insensitive() -> None:
    _, known = calculate_cost(
        "GPT-5.4", input_tokens=1, output_tokens=1, provider="openai"
    )
    assert known is True


def test_price_table_calculator_prices_and_flags_unknown() -> None:
    calc = price_table_calculator("xai")
    cost, known = calc(
        model="grok-4.7",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=0,
    )
    assert known is True
    assert cost == pytest.approx(2.0 + 0.5)

    with pytest.warns(UnknownModelWarning):
        cost, known = calc(
            model="grok-unknown",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    assert (cost, known) == (0.0, False)


@pytest.mark.parametrize(
    ("module", "cls"),
    [
        ("deepseek", "DeepSeekChat"),
        ("grok", "GrokChat"),
        ("groq", "GroqChat"),
        ("together", "TogetherChat"),
        ("fireworks", "FireworksChat"),
        ("perplexity", "PerplexityChat"),
        ("cohere", "CohereChat"),
        ("nvidia", "NVIDIAChat"),
        ("azure", "AzureChat"),
    ],
)
def test_price_table_adapters_have_a_cost_calculator(module: str, cls: str) -> None:
    import importlib

    adapter = getattr(importlib.import_module(f"phoson_llm.chats.{module}"), cls)(
        api_key="k"
    )
    assert adapter._cost_calculator is not None


# ─── Provider-reported cost ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        (0.0123, 0.0123),  # OpenRouter: plain number
        ({"total_cost": 0.0042, "input_tokens_cost": 0.001}, 0.0042),  # Perplexity
        (SimpleNamespace(total_cost="0.5"), 0.5),
        ({"input_tokens_cost": 0.001}, None),
    ],
)
def test_extract_provider_cost_shapes(cost: object, expected: float | None) -> None:
    assert _extract_provider_cost(SimpleNamespace(cost=cost)) == expected


# ─── Cheaper Inference live catalog ──────────────────────────────────────────

CATALOG = {
    "models": [
        {
            "id": "claude-sonnet-5.5",
            "aliases": ["anthropic/claude-sonnet-5-5"],
            "model_type": "text",
            "input_per_million": "1.000000",
            "output_per_million": "5.000000",
            "cache_read_per_million": "0.100000000000000000000000",
            "cache_write_per_million": "1.250000000000000000000000",
        },
        {
            "id": "gpt-5.4",
            "model_type": "text",
            "input_per_million": "2.000000",
            "output_per_million": "10.000000",
            "cache_read_per_million": None,
            "cache_write_per_million": None,
            "above_threshold": {
                "input_token_price_threshold": 1000,
                "applies_when": "prompt_tokens_above_threshold",
                "input_per_million": "4.000000",
                "output_per_million": "20.000000",
                "cache_read_input_per_million": "0.400000",
            },
        },
        {
            "id": "premium-tier",
            "model_type": "text",
            "input_per_million": "1.0",
            "output_per_million": "1.0",
            "above_threshold": {
                "input_token_price_threshold": 10,
                "applies_when": (
                    "prompt_tokens_above_threshold"
                    "_and_supplier_charges_long_context_premium"
                ),
                "input_per_million": "9.0",
                "output_per_million": "9.0",
            },
        },
        {"id": "nano-banana", "model_type": "image", "input_per_million": "1.0"},
        {"id": "broken", "model_type": "text", "input_per_million": "n/a"},
    ]
}


def test_parse_catalog_maps_ids_and_aliases() -> None:
    prices = ci.parse_catalog(CATALOG)
    assert set(prices) == {
        "claude-sonnet-5.5",
        "anthropic/claude-sonnet-5-5",
        "gpt-5.4",
        "premium-tier",
    }
    sonnet = prices["claude-sonnet-5.5"].base
    assert (sonnet.input, sonnet.output, sonnet.cache_read, sonnet.cache_write) == (
        1.0,
        5.0,
        0.1,
        1.25,
    )
    # No published read rate: the provider bills it at 10% of input.
    gpt = prices["gpt-5.4"]
    assert gpt.base.cache_read == pytest.approx(0.2)
    assert gpt.base.cache_write is None
    # A tier conditional on the supplier's own premium is not applied.
    assert prices["premium-tier"].above is None


def test_long_context_tier_applies_above_threshold() -> None:
    gpt = ci.parse_catalog(CATALOG)["gpt-5.4"]
    small = gpt.cost(
        input_tokens=1000, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
    )
    # 900 net + 200 cached = 1100 prompt tokens > 1000: whole request at tier rates.
    large = gpt.cost(
        input_tokens=900, output_tokens=0, cache_read_tokens=200, cache_write_tokens=0
    )
    assert small == pytest.approx(1000 * 2.0 / 1e6)
    assert large == pytest.approx((900 * 4.0 + 200 * 0.4) / 1e6)


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        ci.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )
    return seen


@pytest.fixture
def fresh_catalog(monkeypatch: pytest.MonkeyPatch) -> ci._Catalog:
    catalog = ci._Catalog()
    monkeypatch.setattr(ci, "_CATALOG", catalog)
    return catalog


@pytest.mark.asyncio
async def test_adapter_prices_from_live_catalog(
    monkeypatch: pytest.MonkeyPatch, fresh_catalog: ci._Catalog
) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=CATALOG))
    chat = ci.CheaperInferenceChat(api_key="ci_live_x")
    await chat._catalog.refresh(chat._catalog_url)
    await chat._catalog.refresh(chat._catalog_url)  # cached: no second fetch
    assert len(seen) == 1
    assert "authorization" not in seen[0].headers  # keyless endpoint

    cost, known = chat._catalog_cost(
        model="Claude-Sonnet-5.5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=0,
    )
    assert known is True
    assert cost == pytest.approx(1.0 + 5.0 + 0.1)

    assert chat._catalog_cost(model="unlisted", input_tokens=1, output_tokens=1) == (
        0.0,
        False,
    )


@pytest.mark.asyncio
async def test_catalog_failure_leaves_cost_unknown_and_backs_off(
    monkeypatch: pytest.MonkeyPatch, fresh_catalog: ci._Catalog
) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(503))
    chat = ci.CheaperInferenceChat(api_key="ci_live_x")
    await chat._catalog.refresh(chat._catalog_url)
    await chat._catalog.refresh(chat._catalog_url)  # within the retry window
    assert len(seen) == 1
    assert chat._catalog_cost(
        model="claude-sonnet-5.5", input_tokens=1, output_tokens=1
    ) == (0.0, False)


@pytest.mark.asyncio
async def test_stream_refreshes_catalog_and_reports_cost(
    monkeypatch: pytest.MonkeyPatch, fresh_catalog: ci._Catalog
) -> None:
    from phoson_llm.schemas import Message, UsageEvent, ModelConfig

    _mock_http(monkeypatch, lambda r: httpx.Response(200, json=CATALOG))

    usage = SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    chunk = SimpleNamespace(choices=[], usage=usage)

    class Completions:
        async def create(self, **kwargs):
            async def gen():
                yield chunk

            return gen()

    chat = ci.CheaperInferenceChat(api_key="ci_live_x")
    chat._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))  # type: ignore[assignment]
    events = [
        ev
        async for ev in chat.stream(
            [Message(role="user", content="hi")], ModelConfig(model="gpt-5.4")
        )
    ]
    usage_events = [ev for ev in events if isinstance(ev, UsageEvent)]
    assert usage_events, events
    assert usage_events[0].cost_known is True
    # 1M prompt tokens exceeds the 1000-token threshold: tier input rate.
    assert usage_events[0].cost_usd == pytest.approx(4.0)
