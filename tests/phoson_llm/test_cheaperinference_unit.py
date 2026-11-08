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


# ─── Prompt caching ──────────────────────────────────────────────────────────


class _FakeCompletions:
    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs

        async def _empty():
            if False:
                yield None

        return _empty()


def _fake_chat() -> tuple[CheaperInferenceChat, _FakeCompletions]:
    chat = CheaperInferenceChat(api_key="ci_live_test")
    completions = _FakeCompletions()
    chat._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))  # type: ignore[assignment]
    return chat, completions


def _cache_markers(obj) -> list:
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == "cache_control"] + [
            m for k, v in obj.items() if k != "cache_control" for m in _cache_markers(v)
        ]
    if isinstance(obj, list):
        return [m for item in obj for m in _cache_markers(item)]
    return []


def _two_tool_turn() -> list:
    from phoson_llm.schemas import Message, ToolUseBlock, ToolResultBlock

    return [
        Message(role="user", content="do two things"),
        Message(
            role="assistant",
            content=[ToolUseBlock("id1", "bash", {}), ToolUseBlock("id2", "bash", {})],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock("id1", "out1", error=False),
                ToolResultBlock("id2", "out2", error=False),
            ],
        ),
    ]


async def _send(model: str, session_id: str | None = "sess-1") -> dict:
    from phoson_llm.schemas import ModelConfig

    chat, completions = _fake_chat()
    config = ModelConfig(model=model, system="You are Phos.", session_id=session_id)
    async for _ in chat.stream(_two_tool_turn(), config):
        pass
    assert completions.last_kwargs is not None
    return completions.last_kwargs


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4.6", True),
        ("Claude-Sonnet-4.6", True),
        ("anthropic/claude-haiku-4.5", True),
        ("gpt-5.4", False),
        ("deepseek-v4", False),
        ("glm-5.2", False),
    ],
)
def test_supports_explicit_cache(model: str, expected: bool) -> None:
    from phoson_llm.chats.cheaperinference import supports_explicit_cache

    assert supports_explicit_cache(model) is expected


@pytest.mark.asyncio
async def test_claude_requests_carry_cache_markers() -> None:
    kwargs = await _send("claude-opus-4.6")
    messages = kwargs["messages"]
    # System prefix (tools ride along with it) + the newest message.
    assert messages[0]["role"] == "system"
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert len(_cache_markers(messages)) == 2


@pytest.mark.asyncio
async def test_claude_tool_results_are_not_coalesced() -> None:
    """Fusing tool results is an Alibaba extension; standard tool messages
    (one per tool_call_id) must reach Cheaper Inference unchanged."""
    kwargs = await _send("claude-opus-4.6")
    tool_msgs = [m for m in kwargs["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["id1", "id2"]
    assert all(
        "tool_call_id" not in part
        for m in tool_msgs
        for part in (m["content"] if isinstance(m["content"], list) else [])
    )


@pytest.mark.asyncio
async def test_non_claude_requests_carry_no_markers() -> None:
    kwargs = await _send("gpt-5.4")
    assert _cache_markers(kwargs["messages"]) == []
    assert kwargs["messages"][0]["content"] == "You are Phos."


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-opus-4.6", "gpt-5.4"])
async def test_session_affinity_header(model: str) -> None:
    kwargs = await _send(model)
    assert kwargs["extra_headers"] == {"x-ci-prompt-cache-session": "sess-1"}


@pytest.mark.asyncio
async def test_no_affinity_header_without_session() -> None:
    kwargs = await _send("claude-opus-4.6", session_id=None)
    assert "extra_headers" not in kwargs
