from types import SimpleNamespace

import httpx
import pytest

from phoson_cli.models import ModelOption
from phoson_cli.model_selector import (
    _prioritize_current,
    list_available_models,
    _openrouter_agentic_index,
    _format_openrouter_pricing,
)


@pytest.mark.asyncio
async def test_list_available_models_openrouter_prioritizes_current() -> None:
    config = SimpleNamespace(provider="openrouter", model="openai/gpt-4.1-mini")

    models = await list_available_models(config)

    assert models
    assert models[0].id == "openai/gpt-4.1-mini"
    assert any(m.id == "google/gemini-2.5-flash" for m in models)


@pytest.mark.asyncio
async def test_list_available_models_ollama_falls_back_on_error(monkeypatch) -> None:
    config = SimpleNamespace(
        provider="ollama",
        model="llama3.2",
        ollama_base_url="http://localhost:11434",
    )

    class DummyClient:
        async def __aenter__(self):
            raise httpx.ConnectError("boom")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient", lambda timeout: DummyClient()
    )

    with pytest.warns(UserWarning, match="Failed to fetch Ollama models"):
        models = await list_available_models(config)

    assert [m.id for m in models] == ["llama3.2"]


def test_format_openrouter_pricing_is_human_readable() -> None:
    result = _format_openrouter_pricing(
        {"prompt": "0.000002", "completion": "0.000008"}
    )

    assert result == "in $2/M · out $8/M"


# ── I-113: OpenRouter agentic_index ordering ─────────────────────────────────


_ABSENT = object()


def _agentic_model(id_: str, agentic: object) -> dict:
    item = {"id": id_, "name": id_}
    if agentic is not _ABSENT:
        item["benchmarks"] = {"artificial_analysis": {"agentic_index": agentic}}
    return item


def _openrouter_payload(items: list[dict]) -> "httpx.Response":
    class _Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"data": items}

    return _Response()  # type: ignore[return-value]


def _mock_openrouter(monkeypatch, items: list[dict]) -> None:
    class DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def get(self, url: str, headers: object = None) -> "httpx.Response":
            return _openrouter_payload(items)

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient",
        lambda *args, **kwargs: DummyClient(),
    )


@pytest.mark.asyncio
async def test_openrouter_sorted_by_agentic_index_descending(
    monkeypatch,
) -> None:
    """Models with agentic_index first (high → low); no-field models last,
    alphabetical among themselves."""
    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})
    items = [
        _agentic_model("vendor/c-model", 40),
        _agentic_model("vendor/a-model", 80),
        _agentic_model("vendor/b-model", None),  # benchmarks present, null index
        _agentic_model("vendor/e-model", None),
        _agentic_model("vendor/d-model", 55),
    ]
    _mock_openrouter(monkeypatch, items)
    config = SimpleNamespace(provider="openrouter", model="vendor/a-model")

    models = await list_available_models(config)

    assert [m.id for m in models] == [
        "vendor/a-model",  # 80
        "vendor/d-model",  # 55
        "vendor/c-model",  # 40
        "vendor/b-model",  # no index → last group,
        "vendor/e-model",  # alphabetical within the group
    ]
    assert models[0].agentic_index == 80.0
    assert models[1].agentic_index == 55.0
    assert models[2].agentic_index == 40.0
    assert models[3].agentic_index is None
    assert models[4].agentic_index is None


@pytest.mark.asyncio
async def test_openrouter_current_model_first_even_with_low_index(
    monkeypatch,
) -> None:
    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})
    items = [
        _agentic_model("vendor/current", 1),
        _agentic_model("vendor/top", 99),
    ]
    _mock_openrouter(monkeypatch, items)
    config = SimpleNamespace(provider="openrouter", model="vendor/current")

    models = await list_available_models(config)

    assert [m.id for m in models] == ["vendor/current", "vendor/top"]


def test_prioritize_current_default_stays_alphabetical() -> None:
    """Non-OpenRouter callers pass no order_key → exact previous behaviour."""
    options = [
        ModelOption(id="zeta", label="z", provider="p"),
        ModelOption(id="alpha", label="a", provider="p"),
        ModelOption(id="mid", label="m", provider="p"),
    ]

    result = _prioritize_current(options, "mid")

    assert [m.id for m in result] == ["mid", "alpha", "zeta"]


def test_openrouter_agentic_index_parsing_is_defensive() -> None:
    assert (
        _openrouter_agentic_index(
            {"benchmarks": {"artificial_analysis": {"agentic_index": 72}}}
        )
        == 72.0
    )
    assert (
        _openrouter_agentic_index(
            {"benchmarks": {"artificial_analysis": {"agentic_index": 72.5}}}
        )
        == 72.5
    )
    assert (
        _openrouter_agentic_index(
            {"benchmarks": {"artificial_analysis": {"agentic_index": None}}}
        )
        is None
    )
    assert (
        _openrouter_agentic_index(
            {"benchmarks": {"artificial_analysis": {"agentic_index": "high"}}}
        )
        is None
    )
    assert _openrouter_agentic_index({"benchmarks": None}) is None
    assert _openrouter_agentic_index({"benchmarks": "x"}) is None
    assert _openrouter_agentic_index({}) is None
