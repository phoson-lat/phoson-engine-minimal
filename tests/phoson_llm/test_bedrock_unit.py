"""Unit tests for the Bedrock adapter (boto3 Converse API, mocked)."""

import pytest

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ToolCallDeltaEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.bedrock import (
    BedrockChat,
    _convert_tools_bedrock,
    _convert_messages_bedrock,
)

boto3 = pytest.importorskip("boto3")


# ─── Basic ───────────────────────────────────────────────────────────────────


def test_is_base_llm_chat_subclass():
    chat = BedrockChat(region_name="us-east-1")
    assert isinstance(chat, BaseLLMChat)


def test_repr_includes_bedrock():
    chat = BedrockChat(region_name="us-east-1")
    assert "Bedrock" in repr(chat)


# ─── Message / tool conversion ───────────────────────────────────────────────


def test_convert_messages_handles_tool_use_and_result():
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(tool_call_id="t1", tool_name="bash", args={"cmd": "ls"})
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_call_id="t1", result="ok", error=False)],
        ),
    ]
    out = _convert_messages_bedrock(msgs)
    # system is dropped (it goes in the `system` param, not messages)
    assert out[0] == {"role": "user", "content": [{"text": "hi"}]}
    assert out[1]["role"] == "assistant"
    assert out[1]["content"][0]["toolUse"] == {
        "toolUseId": "t1",
        "name": "bash",
        "input": {"cmd": "ls"},
    }
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["toolResult"] == {
        "toolUseId": "t1",
        "content": [{"text": "ok"}],
        "status": "success",
    }


def test_convert_tools_bedrock_shape():
    tools = [
        ToolDefinition(name="bash", description="run", parameters={"type": "object"})
    ]
    out = _convert_tools_bedrock(tools)
    assert out == [
        {
            "toolSpec": {
                "name": "bash",
                "description": "run",
                "inputSchema": {"json": {"type": "object"}},
            }
        }
    ]


# ─── Streaming (converse_stream) ─────────────────────────────────────────────


def _streaming_client(events):
    """Build a fake boto3 client whose converse_stream yields *events*."""

    class FakeClient:
        def converse_stream(self, **kwargs):
            return {"stream": iter(events)}

    return FakeClient()


def _tool_stream_events():
    return [
        {"contentDelta": {"text": "Let me "}},
        {"contentDelta": {"text": "check."}},
        {
            "toolUse": {
                "toolUseId": "tu1",
                "name": "get_weather",
                "content": {"json": '{"ci'},
            }
        },
        {"toolUse": {"content": {"json": 'ty":"Paris"}'}}},
        {
            "metadata": {
                "stopReason": "tool_use",
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }
        },
    ]


async def test_bedrock_stream_emits_tokens_and_tool_calls():
    chat = BedrockChat(region_name="us-east-1")
    chat._client = _streaming_client(_tool_stream_events())
    cfg = ModelConfig(
        model="anthropic.claude-3-sonnet", temperature=0.5, max_tokens=100
    )

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    types = [type(e) for e in events]
    assert types[0] is LLMStartEvent
    assert types[-1] is LLMDoneEvent
    assert TokenEvent in types
    assert ToolCallDeltaEvent in types
    assert ToolCallEvent in types
    assert UsageEvent in types

    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert "".join(t.content for t in tokens) == "Let me check."

    tool = next(e for e in events if isinstance(e, ToolCallEvent))
    assert tool.tool_name == "get_weather"
    assert tool.tool_call_id == "tu1"
    assert tool.args == {"city": "Paris"}

    done = events[-1]
    assert done.has_tool_calls is True
    assert done.stop_reason == "tool_use"


async def test_bedrock_stream_passes_tool_config():
    captured = {}

    class FakeClient:
        def converse_stream(self, **kwargs):
            captured.update(kwargs)
            return {
                "stream": iter(
                    [
                        {"contentDelta": {"text": "ok"}},
                        {
                            "metadata": {
                                "stopReason": "end_turn",
                                "usage": {"inputTokens": 1, "outputTokens": 1},
                            }
                        },
                    ]
                )
            }

    chat = BedrockChat(region_name="us-east-1")
    chat._client = FakeClient()
    cfg = ModelConfig(model="m", temperature=0.3, max_tokens=50)
    tools = [
        ToolDefinition(name="bash", description="run", parameters={"type": "object"})
    ]

    async for _ in chat.stream([Message(role="user", content="hi")], cfg, tools):
        pass

    assert captured["modelId"] == "m"
    assert captured["system"] == []
    assert captured["toolConfig"] == {
        "tools": [
            {
                "toolSpec": {
                    "name": "bash",
                    "description": "run",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    }
    assert captured["inferenceConfig"] == {"temperature": 0.3, "maxTokens": 50}


async def test_bedrock_stream_system_from_config():
    captured = {}

    class FakeClient:
        def converse_stream(self, **kwargs):
            captured.update(kwargs)
            return {
                "stream": iter(
                    [
                        {
                            "metadata": {
                                "stopReason": "end_turn",
                                "usage": {"inputTokens": 1, "outputTokens": 1},
                            }
                        }
                    ]
                )
            }

    chat = BedrockChat(region_name="us-east-1")
    chat._client = FakeClient()
    cfg = ModelConfig(model="m", system="you are terse", temperature=0.1, max_tokens=10)

    async for _ in chat.stream([Message(role="user", content="hi")], cfg):
        pass

    assert captured["system"] == [{"text": "you are terse"}]


# ─── Non-streaming fallback (converse) ───────────────────────────────────────


def _converse_only_client():
    class FakeClient:
        def converse(self, **kwargs):
            return {
                "output": {
                    "message": {
                        "content": [
                            {"text": "hello"},
                            {
                                "toolUse": {
                                    "toolUseId": "t9",
                                    "name": "search",
                                    "input": {"q": "x"},
                                }
                            },
                        ]
                    }
                },
                "stop_reason": "tool_use",
                "usage": {"inputTokens": 3, "outputTokens": 4},
            }

    return FakeClient()


async def test_bedrock_fallback_to_converse_when_no_stream():
    chat = BedrockChat(region_name="us-east-1")
    chat._client = _converse_only_client()
    cfg = ModelConfig(model="m", temperature=0.2, max_tokens=20)

    with pytest.warns(UserWarning, match="converse_stream"):
        events = [
            e async for e in chat.stream([Message(role="user", content="hi")], cfg)
        ]

    assert any(isinstance(e, TokenEvent) for e in events)
    tool = next(e for e in events if isinstance(e, ToolCallEvent))
    assert tool.tool_name == "search"
    assert tool.tool_call_id == "t9"
    assert tool.args == {"q": "x"}
    done = events[-1]
    assert isinstance(done, LLMDoneEvent)
    assert done.has_tool_calls is True
    assert done.stop_reason == "tool_use"


# ─── Error handling ──────────────────────────────────────────────────────────


async def test_bedrock_emits_error_event_on_exception():
    class FakeClient:
        def converse_stream(self, **kwargs):
            raise RuntimeError("boom")

    chat = BedrockChat(region_name="us-east-1")
    chat._client = FakeClient()
    cfg = ModelConfig(model="m", temperature=0.1, max_tokens=10)

    events = [e async for e in chat.stream([Message(role="user", content="hi")], cfg)]

    assert events[0] is not None
    assert isinstance(events[-1], ErrorEvent)
    assert "boom" in events[-1].message
    assert events[-1].code == "provider_error"
