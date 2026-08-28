import pytest

from phoson_cli.config import PhosonConfig, build_chat
from phoson_llm.schemas import (
    Message,
    TokenUsage,
    UsageEvent,
    ModelConfig,
)
from phoson_llm.chats.openrouter import OpenRouterChat
from phoson_llm.chats._openai_compatible import (
    _parse_tool_args,
    _extract_reasoning_delta,
)


class _Delta:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_build_chat_returns_openrouter_chat_for_openrouter_provider() -> None:
    chat = build_chat(
        PhosonConfig(
            provider="openrouter",
            openrouter_api_key="test-key",
        )
    )

    assert isinstance(chat, OpenRouterChat)


def test_extract_reasoning_delta_supports_reasoning_content() -> None:
    assert (
        _extract_reasoning_delta(_Delta(reasoning_content="step by step"))
        == "step by step"
    )


def test_extract_reasoning_delta_supports_reasoning_alias() -> None:
    assert _extract_reasoning_delta(_Delta(reasoning="trace")) == "trace"


def test_usage_event_can_mark_openrouter_cost_as_unknown() -> None:
    event = UsageEvent(
        model="minimax/minimax-m2.5:free",
        usage=TokenUsage(input=12, output=7),
        cost_usd=0.0,
        cost_known=False,
    )

    assert event.usage.input == 12
    assert event.usage.output == 7
    assert event.cost_usd == 0.0
    assert event.cost_known is False


def test_parse_tool_args_accepts_plain_json_object() -> None:
    assert _parse_tool_args('{"command":"git status"}') == {"command": "git status"}


def test_parse_tool_args_maps_plain_string_to_command() -> None:
    assert _parse_tool_args('"git log -1 --oneline"') == {
        "command": "git log -1 --oneline"
    }


def test_parse_tool_args_maps_invalid_json_to_command_fallback() -> None:
    import pytest

    with pytest.warns(UserWarning, match="Could not parse tool args JSON"):
        assert _parse_tool_args("git diff --stat") == {"command": "git diff --stat"}


def test_openrouter_tool_chunks_can_arrive_before_id_and_name() -> None:
    tool_args_acc: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    tool_ids: dict[int, str] = {}

    idx = 0
    tool_args_acc.setdefault(idx, "")
    tool_args_acc[idx] += '{"city":"Qro"}'

    assert tool_args_acc[idx] == '{"city":"Qro"}'

    tool_ids[idx] = "call_123"
    tool_names[idx] = "get_weather"

    emitted = []
    for current_idx, raw in tool_args_acc.items():
        if current_idx not in tool_ids or current_idx not in tool_names:
            continue
        emitted.append((tool_ids[current_idx], tool_names[current_idx], raw))

    assert emitted == [("call_123", "get_weather", '{"city":"Qro"}')]


# ─── G2: prompt caching (session_id, cache_control, attribution) ────────────


def test_openrouter_sends_default_attribution_headers() -> None:
    """phoson-cli attributes its OpenRouter usage by default, like other
    agent CLIs (HTTP-Referer + X-OpenRouter-Title)."""
    chat = OpenRouterChat(api_key="test-key")
    headers = chat._client.default_headers or {}
    assert headers["HTTP-Referer"] == "https://phoson.lat"
    assert headers["X-OpenRouter-Title"] == "phoson-cli"
    assert "cli-agent" in headers.get("X-OpenRouter-Categories", "")


def test_openrouter_attribution_headers_can_be_overridden() -> None:
    chat = OpenRouterChat(
        api_key="test-key",
        http_referer="https://example.com",
        app_title="my-agent",
    )
    headers = chat._client.default_headers or {}
    assert headers["HTTP-Referer"] == "https://example.com"
    assert headers["X-OpenRouter-Title"] == "my-agent"


class _ORDelta:
    content: str | None = None
    tool_calls = None


class _ORChoice:
    def __init__(self, finish_reason: str | None = None) -> None:
        self.delta = _ORDelta()
        self.finish_reason = finish_reason


class _ORChunk:
    def __init__(self, choices: list | None = None, usage=None) -> None:
        self.choices = choices or []
        self.usage = usage


class _ORCompletions:
    """Captures the request kwargs of ``chat.completions.create``."""

    def __init__(self, captured: dict) -> None:
        self.captured = captured

    async def create(self, **kwargs):
        self.captured.clear()
        self.captured.update(kwargs)

        async def _gen():
            yield _ORChunk(choices=[_ORChoice("stop")])

        return _gen()


class _ORChatNamespace:
    def __init__(self, captured: dict) -> None:
        self.completions = _ORCompletions(captured)


class _ORClient:
    def __init__(self, captured: dict) -> None:
        self.chat = _ORChatNamespace(captured)


@pytest.mark.asyncio
async def test_openrouter_forwards_session_id_for_sticky_routing() -> None:

    captured: dict = {}
    chat = OpenRouterChat(api_key="test-key")
    chat._client = _ORClient(captured)

    async for _ in chat.stream(
        [Message(role="user", content="hi")],
        ModelConfig(model="anthropic/claude-sonnet-4-6", session_id="sess-123"),
    ):
        pass

    # session_id/cache_control travel inside extra_body — the OpenAI SDK's
    # chat.completions.create() only recognizes the fields it declares as
    # top-level kwargs, so anything else has to go through extra_body to
    # reach the request payload rather than being silently dropped.
    extra_body = captured["extra_body"]
    assert extra_body["session"] == "sess-123"
    # Anthropic route → automatic caching enabled.
    assert extra_body["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_openrouter_omits_cache_fields_when_not_applicable() -> None:

    captured: dict = {}
    chat = OpenRouterChat(api_key="test-key")
    chat._client = _ORClient(captured)

    async for _ in chat.stream(
        [Message(role="user", content="hi")],
        ModelConfig(model="openai/gpt-4o"),
    ):
        pass

    # Neither field applies: extra_body is never even set.
    assert "extra_body" not in captured


@pytest.mark.asyncio
async def test_openrouter_sends_cache_control_for_anthropic_without_session() -> None:

    captured: dict = {}
    chat = OpenRouterChat(api_key="test-key")
    chat._client = _ORClient(captured)

    async for _ in chat.stream(
        [Message(role="user", content="hi")],
        ModelConfig(model="anthropic/claude-haiku-4-5"),
    ):
        pass

    extra_body = captured["extra_body"]
    assert extra_body["cache_control"] == {"type": "ephemeral"}
    assert "session" not in extra_body
