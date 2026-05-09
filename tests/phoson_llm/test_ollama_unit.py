import json
from collections.abc import AsyncIterator

import pytest

from phoson_cli.config import PhosonConfig, build_chat
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_llm.chats.ollama import (
    OllamaChat,
    _convert_tools,
    _prepend_system,
    _convert_messages,
    _is_retryable_status,
)


def test_build_chat_returns_ollama_chat_for_ollama_provider() -> None:
    chat = build_chat(
        PhosonConfig(
            provider="ollama",
            model="llama3",
        )
    )

    assert isinstance(chat, OllamaChat)


def test_convert_messages_keeps_system_in_messages() -> None:
    """Ollama expects ``system`` as a regular message, not a top-level field."""
    messages = [
        Message(role="system", content="You are a helpful assistant"),
        Message(role="user", content="Hello"),
    ]

    result = _convert_messages(messages)

    assert result[0]["role"] == "system"
    assert result[0]["content"] == "You are a helpful assistant"
    assert result[1]["role"] == "user"
    assert result[1]["content"] == "Hello"


def test_convert_tools_formats_correctly() -> None:
    tools = [
        ToolDefinition(
            name="get_weather",
            description="Get weather for a location",
            parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        )
    ]

    result = _convert_tools(tools)

    assert len(result) == 1
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "get_weather"
    assert result[0]["function"]["description"] == "Get weather for a location"


def test_ollama_chat_default_base_url() -> None:
    chat = OllamaChat()

    assert chat._base_url == "http://localhost:11434"


def test_ollama_chat_custom_base_url() -> None:
    chat = OllamaChat(base_url="http://192.168.1.100:11434")

    assert chat._base_url == "http://192.168.1.100:11434"


def test_prepend_system_adds_system_at_start() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    out = _prepend_system(msgs, "be nice")
    assert out[0] == {"role": "system", "content": "be nice"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_prepend_system_replaces_existing() -> None:
    msgs = [
        {"role": "system", "content": "old"},
        {"role": "user", "content": "hi"},
    ]
    out = _prepend_system(msgs, "new")
    assert out[0] == {"role": "system", "content": "new"}
    assert len(out) == 2


def test_is_retryable_status_5xx_and_429() -> None:
    assert _is_retryable_status(429) is True
    assert _is_retryable_status(500) is True
    assert _is_retryable_status(503) is True
    assert _is_retryable_status(599) is True
    assert _is_retryable_status(404) is False
    assert _is_retryable_status(401) is False


# ─── Streaming protocol tests ────────────────────────────────────────────────


class _FakeOllamaResponse:
    """Minimal async-iter response stub that mimics ``httpx`` streaming."""

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aiter_bytes(self) -> AsyncIterator[bytes]:  # for error path
        for line in self._lines:
            yield line.encode("utf-8")

    async def __aenter__(self) -> "_FakeOllamaResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeClient:
    def __init__(self, response: _FakeOllamaResponse) -> None:
        self._response = response
        self.last_payload: dict | None = None

    def stream(self, method: str, url: str, json: dict) -> _FakeOllamaResponse:
        self.last_payload = json
        return self._response

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    import phoson_llm.chats.ollama as mod

    def _factory(*args: object, **kwargs: object) -> _FakeClient:
        return client

    monkeypatch.setattr(mod.httpx, "AsyncClient", _factory)


async def _collect(it: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_stream_emits_usage_on_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``done: true`` line carries usage; UsageEvent must be emitted."""
    lines = [
        json.dumps(
            {
                "model": "llama3",
                "message": {"role": "assistant", "content": "Hi "},
                "done": False,
            }
        ),
        json.dumps(
            {
                "model": "llama3",
                "message": {"role": "assistant", "content": "there"},
                "done": False,
            }
        ),
        json.dumps(
            {
                "model": "llama3",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 10,
                "eval_count": 5,
            }
        ),
    ]
    client = _FakeClient(_FakeOllamaResponse(lines))
    _patch_httpx(monkeypatch, client)

    chat = OllamaChat()
    config = ModelConfig(model="llama3", max_tokens=128)
    events = await _collect(chat.stream([Message(role="user", content="hi")], config))

    types = [type(e).__name__ for e in events]
    assert "LLMStartEvent" in types
    assert types.count("TokenEvent") == 2
    assert "UsageEvent" in types
    assert types[-1] == "LLMDoneEvent"

    usage_event = next(e for e in events if isinstance(e, UsageEvent))
    assert usage_event.usage.input == 10
    assert usage_event.usage.output == 5
    assert usage_event.cost_known is False

    done = next(e for e in events if isinstance(e, LLMDoneEvent))
    assert done.content == "Hi there"
    assert done.has_tool_calls is False


@pytest.mark.asyncio
async def test_stream_sends_system_in_messages_not_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system prompt must be prepended as a message, never top-level."""
    lines = [
        json.dumps(
            {
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "eval_count": 1,
                "prompt_eval_count": 1,
            }
        )
    ]
    client = _FakeClient(_FakeOllamaResponse(lines))
    _patch_httpx(monkeypatch, client)

    chat = OllamaChat()
    config = ModelConfig(model="llama3", max_tokens=64, system="be helpful")
    await _collect(chat.stream([Message(role="user", content="hi")], config))

    payload = client.last_payload
    assert payload is not None
    assert "system" not in payload, "system must not be a top-level field"
    assert payload["messages"][0] == {"role": "system", "content": "be helpful"}
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_stream_forwards_max_tokens_via_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: max_tokens must always be forwarded as ``num_predict``.

    Old code skipped the field when ``max_tokens == 4096`` which never
    matched the real default (32768) and was a confused heuristic.
    """
    lines = [
        json.dumps(
            {
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "eval_count": 1,
                "prompt_eval_count": 1,
            }
        )
    ]

    for max_tokens in (4096, 8192, 32 * 1024):
        client = _FakeClient(_FakeOllamaResponse(lines))
        _patch_httpx(monkeypatch, client)
        chat = OllamaChat()
        config = ModelConfig(model="llama3", max_tokens=max_tokens)
        await _collect(chat.stream([Message(role="user", content="hi")], config))
        payload = client.last_payload
        assert payload is not None
        assert payload["options"]["num_predict"] == max_tokens


@pytest.mark.asyncio
async def test_stream_emits_tool_call_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool calls must produce delta events and a final ToolCallEvent."""
    lines = [
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "get_weather",
                                "arguments": {"city": "Querétaro"},
                            },
                        }
                    ],
                },
                "done": False,
            }
        ),
        json.dumps(
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "tool_calls",
                "eval_count": 7,
                "prompt_eval_count": 12,
            }
        ),
    ]
    client = _FakeClient(_FakeOllamaResponse(lines))
    _patch_httpx(monkeypatch, client)

    chat = OllamaChat()
    config = ModelConfig(model="llama3", max_tokens=128)
    events = await _collect(
        chat.stream([Message(role="user", content="weather?")], config)
    )

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "get_weather"
    assert tool_events[0].args == {"city": "Querétaro"}

    done = next(e for e in events if isinstance(e, LLMDoneEvent))
    assert done.has_tool_calls is True


@pytest.mark.asyncio
async def test_stream_yields_error_with_body_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeOllamaResponse(
        ['{"error":"model not found"}'],
        status_code=404,
    )
    client = _FakeClient(response)
    _patch_httpx(monkeypatch, client)

    chat = OllamaChat()
    config = ModelConfig(model="missing", max_tokens=64)
    events = await _collect(chat.stream([Message(role="user", content="hi")], config))

    types = [type(e).__name__ for e in events]
    assert "ErrorEvent" in types
    err = next(e for e in events if type(e).__name__ == "ErrorEvent")
    assert "404" in err.message  # type: ignore[attr-defined]
    # 404 is not retryable, 5xx and 429 are.
    assert err.retryable is False  # type: ignore[attr-defined]
