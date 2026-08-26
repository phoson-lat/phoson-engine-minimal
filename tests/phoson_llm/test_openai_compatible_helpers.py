"""Direct unit tests for the shared OpenAI-compatible helpers.

These exercise the conversion/accumulation utilities in
``phoson_llm/chats/_openai_compatible.py`` directly (not through the
adapter classes), so regressions in the shared logic are caught without
spinning up clients or streams.
"""

import json
import warnings
from types import SimpleNamespace

import pytest

from phoson_llm.schemas import (
    Message,
    TextBlock,
    ModelConfig,
    ToolUseBlock,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ToolCallDeltaEvent,
)
from phoson_llm.chats._openai_compatible import (
    ToolCallAccumulator,
    _parse_tool_args,
    _convert_messages,
    _build_request_kwargs,
    _extract_reasoning_delta,
)

# ── _parse_tool_args ─────────────────────────────────────────────────────────


class TestParseToolArgs:
    def test_valid_json_object(self):
        assert _parse_tool_args(json.dumps({"x": 1})) == {"x": 1}

    def test_empty_string_returns_empty_dict(self):
        assert _parse_tool_args("") == {}

    def test_invalid_json_falls_back_to_command_with_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _parse_tool_args("{not json")
        assert result == {"command": "{not json"}
        assert any("Could not parse tool args JSON" in str(w.message) for w in caught)

    def test_json_string_wrapped_as_command(self):
        assert _parse_tool_args(json.dumps("hello")) == {"command": "hello"}

    def test_unexpected_json_type_stored_as_raw(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _parse_tool_args(json.dumps(42))
        assert result == {"_raw": "42"}
        assert any("Unexpected tool args type" in str(w.message) for w in caught)


# ── _extract_reasoning_delta ─────────────────────────────────────────────────


class TestExtractReasoningDelta:
    def test_reasoning_content_attribute(self):
        delta = SimpleNamespace(reasoning_content="thinking...")
        assert _extract_reasoning_delta(delta) == "thinking..."

    def test_reasoning_attribute_variant(self):
        delta = SimpleNamespace(reasoning="chain of thought")
        assert _extract_reasoning_delta(delta) == "chain of thought"

    def test_no_reasoning_attributes(self):
        delta = SimpleNamespace(content="plain")
        assert _extract_reasoning_delta(delta) is None

    def test_empty_reasoning_is_ignored(self):
        delta = SimpleNamespace(reasoning_content="", reasoning=None)
        assert _extract_reasoning_delta(delta) is None


# ── ToolCallAccumulator ──────────────────────────────────────────────────────


def _tc(
    index: int, id: str | None = None, name: str | None = None, args: str | None = None
):
    """Build a minimal stand-in for an SDK ``delta.tool_calls`` entry."""
    function = SimpleNamespace(name=name, arguments=args) if (name or args) else None
    return SimpleNamespace(index=index, id=id, function=function)


class TestToolCallAccumulator:
    def test_single_chunk_with_id_name_and_args(self):
        acc = ToolCallAccumulator()
        delta_events = acc.feed_delta(_tc(0, id="call_1", name="calc", args='{"x": 2}'))
        assert len(delta_events) == 1
        assert isinstance(delta_events[0], ToolCallDeltaEvent)
        assert delta_events[0].args_chunk == '{"x": 2}'
        assert delta_events[0].tool_name == "calc"

        final = acc.finalize()
        assert len(final) == 1
        assert isinstance(final[0], ToolCallEvent)
        assert final[0].tool_call_id == "call_1"
        assert final[0].tool_name == "calc"
        assert final[0].args == {"x": 2}

    def test_args_accumulate_across_chunks(self):
        acc = ToolCallAccumulator()
        acc.feed_delta(_tc(0, id="call_1", name="write_file", args='{"path": '))
        acc.feed_delta(_tc(0, args='"a.txt", '))
        acc.feed_delta(_tc(0, args='"content": "hi"}'))

        final = acc.finalize()
        assert final[0].args == {"path": "a.txt", "content": "hi"}

    def test_finalize_resets_buffers(self):
        acc = ToolCallAccumulator()
        acc.feed_delta(_tc(0, id="call_1", name="t", args="{}"))
        assert len(acc.finalize()) == 1
        # A second finalize with no new data produces nothing.
        assert acc.finalize() == []

    def test_call_without_id_or_name_is_skipped(self):
        acc = ToolCallAccumulator()
        acc.feed_delta(_tc(0, args='{"x": 1}'))  # no id, no name
        assert acc.finalize() == []

    def test_delta_without_args_chunk_emits_nothing(self):
        acc = ToolCallAccumulator()
        assert acc.feed_delta(_tc(0, id="call_1", name="t")) == []


# ── _build_request_kwargs ────────────────────────────────────────────────────


class TestBuildRequestKwargs:
    def test_max_tokens_key_variants(self):
        cfg = ModelConfig(model="m", max_tokens=128)
        messages = [Message(role="user", content="hi")]

        kwargs_default = _build_request_kwargs(
            config=cfg, messages=messages, tools=None, max_tokens_key="max_tokens"
        )
        kwargs_completion = _build_request_kwargs(
            config=cfg,
            messages=messages,
            tools=None,
            max_tokens_key="max_completion_tokens",
        )

        assert kwargs_default["max_tokens"] == 128
        assert "max_tokens" not in kwargs_completion
        assert kwargs_completion["max_completion_tokens"] == 128

    def test_system_config_prepended_and_deduped(self):
        cfg = ModelConfig(model="m", system="be brief")
        messages = [
            Message(role="system", content="stale system"),
            Message(role="user", content="hi"),
        ]
        kwargs = _build_request_kwargs(
            config=cfg, messages=messages, tools=None, max_tokens_key="max_tokens"
        )
        roles = [m["role"] for m in kwargs["messages"]]
        assert roles[0] == "system"
        assert kwargs["messages"][0]["content"] == "be brief"
        assert roles.count("system") == 1

    def test_reasoning_effort_drops_temperature(self):
        cfg = ModelConfig(model="o3", reasoning_effort="high")
        messages = [Message(role="user", content="hi")]
        kwargs = _build_request_kwargs(
            config=cfg, messages=messages, tools=None, max_tokens_key="max_tokens"
        )
        assert kwargs["reasoning_effort"] == "high"
        assert "temperature" not in kwargs

    @pytest.mark.parametrize("effort", ["xhigh", "max"])
    def test_extended_reasoning_effort_forwarded_as_is(self, effort):
        cfg = ModelConfig(model="qwen/qwen3.6-plus", reasoning_effort=effort)
        messages = [Message(role="user", content="hi")]
        kwargs = _build_request_kwargs(
            config=cfg, messages=messages, tools=None, max_tokens_key="max_tokens"
        )
        assert kwargs["reasoning_effort"] == effort
        assert "temperature" not in kwargs

    def test_tools_are_converted(self):
        cfg = ModelConfig(model="m")
        tools = [
            ToolDefinition(
                name="calc", description="calc", parameters={"type": "object"}
            )
        ]
        kwargs = _build_request_kwargs(
            config=cfg,
            messages=[Message(role="user", content="hi")],
            tools=tools,
            max_tokens_key="max_tokens",
        )
        assert kwargs["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "calc",
                    "description": "calc",
                    "parameters": {"type": "object"},
                },
            }
        ]


# ── _convert_messages ────────────────────────────────────────────────────────


class TestConvertMessages:
    def test_string_content(self):
        out = _convert_messages(
            [
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi there"),
            ]
        )
        assert out == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_tool_use_and_tool_result_blocks(self):
        out = _convert_messages(
            [
                Message(
                    role="assistant",
                    content=[
                        TextBlock(text="let me check"),
                        ToolUseBlock(
                            tool_call_id="call_1", tool_name="calc", args={"x": 1}
                        ),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_call_id="call_1", result="42", error=False
                        ),
                    ],
                ),
            ]
        )
        # Tool uses become OpenAI-style tool_calls on the assistant message.
        assistant = out[0]
        assert assistant["role"] == "assistant"
        assert assistant["content"] == "let me check"
        assert assistant["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "calc", "arguments": json.dumps({"x": 1})},
            }
        ]

        # Tool results become dedicated role: "tool" messages.
        tool_msg = out[1]
        assert tool_msg == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "42",
        }
