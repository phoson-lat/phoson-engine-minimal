"""Cheaper Inference provider: adapter defaults, factory/config wiring, model
listing (chat models only) and provider resolution."""

from types import SimpleNamespace

import pytest

from phoson_llm import build_chat
from phoson_cli.config import PhosonConfig
from phoson_cli.config import build_chat as build_cli_chat
from phoson_cli.labels import provider_label
from phoson_cli.models import (
    KNOWN_PROVIDERS,
    ROUTER_PROVIDERS,
    model_provider_for,
    normalize_provider,
)
from phoson_llm.chats.cheaperinference import CheaperInferenceChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat


def test_adapter_defaults() -> None:
    chat = CheaperInferenceChat(api_key="ci_live_test")
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == "https://api.cheaperinference.com/v1"
    assert "Cheaper Inference" in repr(chat)


def test_factory_and_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEAPER_INFERENCE_API_KEY", "ci_live_env")
    chat = build_chat("cheaperinference")
    assert isinstance(chat, CheaperInferenceChat)


def test_cli_config_builds_the_adapter() -> None:
    config = PhosonConfig(
        provider="cheaperinference", cheaperinference_api_key="ci_live_x"
    )
    chat = build_cli_chat(config)
    inner = getattr(chat, "_inner", chat)  # build_chat wraps in RetryingChat
    assert isinstance(inner, CheaperInferenceChat)


def test_names_aliases_and_router_semantics() -> None:
    assert "cheaperinference" in KNOWN_PROVIDERS
    assert normalize_provider("Cheaper-Inference") == "cheaperinference"
    assert normalize_provider("cheaper_inference") == "cheaperinference"
    assert provider_label("cheaperinference") == "Cheaper Inference"
    # It serves other vendors' models: a vendor prefix must not switch providers.
    assert "cheaperinference" in ROUTER_PROVIDERS
    assert model_provider_for("anthropic/claude-opus-4.6", "cheaperinference") is None


@pytest.mark.asyncio
async def test_model_listing_keeps_chat_models_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from phoson_cli.model_selector import list_available_models

    payload = {
        "data": [
            {"id": "claude-opus-4.6", "name": "Claude Opus 4.6", "type": "chat"},
            {"id": "glm-5.2", "type": "chat"},
            {"id": "nano-banana", "type": "image"},
            {"id": "whisper-large-v3-turbo", "type": "stt"},
            {"id": "rerank-v3.5", "type": "rerank"},
            {"id": "gpt-5.4"},
        ]
    }
    seen: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers):
            seen["url"], seen["headers"] = url, headers
            return Response()

    monkeypatch.setattr(
        "phoson_cli.model_selector.httpx.AsyncClient", lambda timeout: Client()
    )
    config = SimpleNamespace(
        provider="cheaperinference",
        model="gpt-5.4",
        cheaperinference_api_key="ci_live_k",
    )

    models = await list_available_models(config)

    assert seen["url"] == "https://api.cheaperinference.com/v1/models"
    assert seen["headers"] == {"Authorization": "Bearer ci_live_k"}
    assert [m.id for m in models][0] == "gpt-5.4"  # current model first
    assert sorted(m.id for m in models) == ["claude-opus-4.6", "glm-5.2", "gpt-5.4"]
    assert all(m.provider == "cheaperinference" for m in models)
