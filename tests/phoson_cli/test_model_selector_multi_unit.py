"""I-113 F2: ModelListingError + list_models_for_providers (concurrent)."""

from types import SimpleNamespace

import httpx
import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.model_selector import (
    ProviderListing,
    ModelListingError,
    list_models_for_providers,
)


def _ok_client(payload: dict):
    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return payload

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.url = None

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def get(self, url: str, headers: object = None) -> "_Resp":
            self.url = url
            return _Resp()

    return _Client()


def _fail_client(exc: Exception):
    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._exc = exc

        async def __aenter__(self) -> "_Client":
            raise self._exc

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    return _Client()


@pytest.mark.asyncio
async def test_list_models_for_providers_aggregates_active_first(
    monkeypatch,
) -> None:
    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})
    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient",
        lambda *a, **k: _ok_client(
            {"data": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}
        ),
    )
    config = PhosonConfig(provider="openrouter", model="x", anthropic_api_key="k")

    listings = await list_models_for_providers(config, ["anthropic", "openrouter"])

    assert [x.provider for x in listings] == ["openrouter", "anthropic"]
    assert all(x.available for x in listings)
    assert [m.id for m in listings[0].options] == ["x", "a", "b"]
    assert [m.id for m in listings[1].options] == ["x", "a", "b"]


@pytest.mark.asyncio
async def test_list_models_for_providers_marks_unavailable_on_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})

    calls: list[str] = []

    class SelectiveClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "SelectiveClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def get(self, url: str, headers: object = None) -> object:
            calls.append(url)
            if "anthropic.com" in url:
                raise httpx.ConnectError("boom")

            class _Resp:
                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict:
                    return {"data": [{"id": "m1", "name": "M1"}]}

            return _Resp()

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient",
        lambda *a, **k: SelectiveClient(),
    )
    config = PhosonConfig(provider="openrouter", model="x", anthropic_api_key="k")

    listings = await list_models_for_providers(config, ["openrouter", "anthropic"])

    or_, an = listings
    assert or_.provider == "openrouter" and or_.available
    assert [m.id for m in or_.options] == ["x", "m1"]
    assert an.provider == "anthropic"
    assert not an.available
    assert an.options == []
    assert "anthropic" in an.error.lower() or "boom" in an.error
    # Both fetched (concurrent), openrouter succeeded while anthropic failed.
    assert any("openrouter.ai" in u for u in calls)
    assert any("anthropic.com" in u for u in calls)


@pytest.mark.asyncio
async def test_list_models_for_providers_runs_concurrently(monkeypatch) -> None:
    """Two slow listers overlap in flight → total < sum of individual times."""
    import asyncio

    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})
    in_flight = 0
    max_in_flight = 0

    class SlowClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "SlowClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def get(self, url: str, headers: object = None) -> object:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1

            class _Resp:
                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict:
                    return {"data": [{"id": "m", "name": "M"}]}

            return _Resp()

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient",
        lambda *a, **k: SlowClient(),
    )
    config = PhosonConfig(
        provider="openrouter",
        model="x",
        groq_api_key="k",
        deepseek_api_key="k",
    )

    listings = await list_models_for_providers(
        config, ["openrouter", "groq", "deepseek"]
    )

    assert len(listings) == 3
    assert max_in_flight >= 2, "listers did not overlap (not concurrent)"
    assert all(x.available for x in listings)


@pytest.mark.asyncio
async def test_list_models_for_providers_unknown_provider_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})
    config = PhosonConfig(provider="openrouter", model="x")

    listings = await list_models_for_providers(config, ["not-a-provider"])

    assert len(listings) == 1
    assert not listings[0].available
    assert "unknown" in listings[0].error.lower()


@pytest.mark.asyncio
async def test_lister_raises_model_listing_error(monkeypatch) -> None:
    from phoson_cli.model_selector import _list_openai_models

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient",
        lambda *a, **k: _fail_client(httpx.ConnectError("boom")),
    )
    config = SimpleNamespace(provider="openai", model="gpt-4o", openai_api_key="k")

    with pytest.raises(ModelListingError, match="OpenAI"):
        await _list_openai_models(config)


@pytest.mark.asyncio
async def test_single_provider_fallback_unchanged_on_error(
    monkeypatch,
) -> None:
    """Fast path: ModelListingError → UserWarning + 1-model fallback (old
    behaviour preserved)."""
    from phoson_cli.model_selector import list_available_models

    monkeypatch.setattr("phoson_cli.model_selector.load_models_file", lambda: {})
    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient",
        lambda *a, **k: _fail_client(httpx.ConnectError("boom")),
    )
    config = SimpleNamespace(provider="openai", model="gpt-4o", openai_api_key="k")

    with pytest.warns(UserWarning, match="Failed to fetch OpenAI models"):
        models = await list_available_models(config)

    assert [m.id for m in models] == ["gpt-4o"]


def test_provider_listing_defaults() -> None:
    listing = ProviderListing(provider="openrouter")
    assert listing.available
    assert listing.options == []
    assert listing.error == ""
