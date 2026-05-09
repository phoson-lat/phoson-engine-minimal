from types import SimpleNamespace

import httpx
import pytest

from phoson_cli.model_selector import list_available_models, _format_openrouter_pricing


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
