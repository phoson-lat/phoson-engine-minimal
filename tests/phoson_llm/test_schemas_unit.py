"""Unit tests for phoson_llm.schemas — inputs and outputs dataclasses."""

import datetime

import pytest

from phoson_llm.schemas import (
    Message,
    ModelConfig,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ImageBlock,
    AudioBlock,
    VideoBlock,
    DocumentBlock,
    ToolDefinition,
    LLMStartEvent,
    LLMDoneEvent,
    TokenEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
    ReasoningDoneEvent,
    ToolCallEvent,
    ToolCallDeltaEvent,
    TokenUsage,
    UsageEvent,
    ErrorEvent,
    LLMModalitiesEvent,
)


# ── inputs ───────────────────────────────────────────────────────────────────


class TestMessage:
    def test_string_content(self):
        m = Message(role="user", content="hello")
        assert m.role == "user"
        assert m.content == "hello"

    def test_block_list_content(self):
        blocks = [TextBlock(text="hi"), ImageBlock(source="http://img.png")]
        m = Message(role="assistant", content=blocks)
        assert len(m.content) == 2  # type: ignore[arg-type]

    def test_system_role(self):
        m = Message(role="system", content="You are helpful.")
        assert m.role == "system"


class TestContentBlocks:
    def test_text_block(self):
        b = TextBlock(text="hello world")
        assert b.text == "hello world"

    def test_tool_use_block(self):
        b = ToolUseBlock(tool_call_id="c1", tool_name="search", args={"q": "python"})
        assert b.tool_call_id == "c1"
        assert b.args == {"q": "python"}

    def test_tool_result_block_defaults(self):
        b = ToolResultBlock(tool_call_id="c1", result="ok")
        assert b.error is False

    def test_tool_result_block_error(self):
        b = ToolResultBlock(tool_call_id="c1", result="boom", error=True)
        assert b.error is True

    def test_image_block_defaults(self):
        b = ImageBlock(source="https://example.com/img.png")
        assert b.detail == "auto"
        assert b.media_type is None

    def test_image_block_custom_detail(self):
        b = ImageBlock(source="data:image/png;base64,abc", detail="high")
        assert b.detail == "high"

    def test_audio_block_defaults(self):
        b = AudioBlock(source="file://audio.wav")
        assert b.format == "wav"
        assert b.duration_ms is None

    def test_video_block_defaults(self):
        b = VideoBlock(source="file://clip.mp4")
        assert b.sampling_interval_ms == 2000

    def test_document_block(self):
        b = DocumentBlock(source="file://doc.pdf", pages=5)
        assert b.pages == 5


class TestToolDefinition:
    def test_construction(self):
        td = ToolDefinition(
            name="calculator",
            description="Does math",
            parameters={"type": "object", "properties": {}},
        )
        assert td.name == "calculator"
        assert td.parameters["type"] == "object"


class TestModelConfig:
    def test_defaults(self):
        cfg = ModelConfig(model="gpt-4o")
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 32 * 1024
        assert cfg.system is None
        assert cfg.thinking_budget is None
        assert cfg.reasoning_effort is None

    def test_custom_values(self):
        cfg = ModelConfig(
            model="claude-3-5-sonnet-20241022",
            temperature=0.0,
            max_tokens=1024,
            system="Be concise.",
        )
        assert cfg.temperature == 0.0
        assert cfg.system == "Be concise."


# ── outputs ──────────────────────────────────────────────────────────────────


class TestLLMEvents:
    def test_event_has_timestamp(self):
        e = TokenEvent(content="hi")
        assert isinstance(e.timestamp, datetime.datetime)
        assert e.timestamp.tzinfo is not None  # UTC-aware

    def test_llm_start_event_defaults(self):
        e = LLMStartEvent()
        assert e.model == ""
        assert e.message_count == 0

    def test_llm_done_event(self):
        e = LLMDoneEvent(content="answer", has_tool_calls=True)
        assert e.content == "answer"
        assert e.has_tool_calls is True

    def test_token_event(self):
        e = TokenEvent(content="chunk")
        assert e.content == "chunk"

    def test_reasoning_events(self):
        assert ReasoningStartEvent()
        assert ReasoningTokenEvent(content="<think>").content == "<think>"
        assert ReasoningDoneEvent(content="done").content == "done"

    def test_tool_call_delta_event(self):
        e = ToolCallDeltaEvent(index=1, tool_name="search", args_chunk='{"q"')
        assert e.index == 1
        assert e.args_chunk == '{"q"'

    def test_tool_call_event_defaults(self):
        e = ToolCallEvent()
        assert e.args == {}
        assert e.tool_call_id == ""

    def test_tool_call_event_with_args(self):
        e = ToolCallEvent(tool_call_id="c1", tool_name="calc", args={"x": 1})
        assert e.args == {"x": 1}

    def test_error_event_defaults(self):
        e = ErrorEvent(message="oops")
        assert e.retryable is False
        assert e.code is None

    def test_error_event_retryable(self):
        e = ErrorEvent(message="rate limit", code="rate_limit", retryable=True)
        assert e.retryable is True

    def test_token_usage_defaults(self):
        u = TokenUsage()
        assert u.input == 0
        assert u.output == 0
        assert u.cache_write == 0
        assert u.cache_read == 0

    def test_usage_event(self):
        e = UsageEvent(
            model="gpt-4o", usage=TokenUsage(input=10, output=20), cost_usd=0.001
        )
        assert e.usage.input == 10
        assert e.cost_usd == 0.001

    def test_modalities_event(self):
        e = LLMModalitiesEvent(supported=["text", "vision"])
        assert "vision" in e.supported
