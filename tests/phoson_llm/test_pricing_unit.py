import pytest

from phoson_llm.pricing import calculate_cost


def test_calculate_cost_known_model_with_cache_tokens() -> None:
    cost_usd, cost_known = calculate_cost(
        model="openai/gpt-4o-mini",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
    )

    assert cost_known is True
    assert cost_usd == pytest.approx(0.000465, abs=1e-9)


def test_calculate_cost_resolves_alias() -> None:
    cost_usd, cost_known = calculate_cost(
        model="anthropic/claude-sonnet-4-6-20250514",
        input_tokens=1000,
        output_tokens=500,
    )

    assert cost_known is True
    assert cost_usd == pytest.approx(0.0105, abs=1e-9)


def test_calculate_cost_unknown_model_returns_not_known() -> None:
    cost_usd, cost_known = calculate_cost(
        model="local/unknown-model",
        input_tokens=1000,
        output_tokens=500,
    )

    assert cost_known is False
    assert cost_usd == 0.0


def test_calculate_cost_known_unprefixed_model_with_provider() -> None:
    cost_usd, cost_known = calculate_cost(
        model="claude-haiku-4-5",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=500,
    )

    assert cost_known is True
    assert cost_usd == pytest.approx(0.0035, abs=1e-9)
