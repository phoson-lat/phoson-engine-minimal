"""Tests for summarization middleware and token estimator."""

from phoson_llm.schemas import Message, TextBlock, ToolUseBlock, ToolResultBlock
from phoson_agent.plugins.summarizer import TokenEstimator, _format_messages_for_summary

# ── TokenEstimator ──────────────────────────────────────────────────


class TestTokenEstimator:
    def test_count_text_basic(self):
        est = TokenEstimator(provider="openai")
        tokens = est.count_text("hello world")
        assert tokens == 2  # "hello" + " world"

    def test_count_text_empty(self):
        est = TokenEstimator(provider="openai")
        assert est.count_text("") == 0

    def test_count_text_longer(self):
        est = TokenEstimator(provider="openai")
        # "The quick brown fox jumps over the lazy dog" = 9 tokens in o200k
        tokens = est.count_text("The quick brown fox jumps over the lazy dog")
        assert tokens > 0

    def test_count_messages_string_content(self):
        est = TokenEstimator(provider="openai")
        messages = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there"),
        ]
        tokens = est.count_messages(messages)
        # 2 messages * 4 overhead + tokens for "Hello" + "Hi there"
        assert tokens > 8  # at least the overhead

    def test_count_messages_block_content(self):
        est = TokenEstimator(provider="openai")
        messages = [
            Message(role="user", content="Run this"),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="Sure"),
                    ToolUseBlock(
                        tool_call_id="t1",
                        tool_name="bash",
                        args={"command": "echo hi"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id="t1",
                        result="hi",
                        error=False,
                    ),
                ],
            ),
        ]
        tokens = est.count_messages(messages)
        assert tokens > 0

    def test_count_messages_grows_with_content(self):
        est = TokenEstimator(provider="openai")
        short = [Message(role="user", content="hi")]
        long = [Message(role="user", content="A" * 1000)]
        assert est.count_messages(long) > est.count_messages(short)

    def test_different_providers_different_encodings(self):
        est_oi = TokenEstimator(provider="openai")
        est_an = TokenEstimator(provider="anthropic")
        # Both should produce reasonable counts (not necessarily equal)
        text = "The meaning of life is 42"
        assert est_oi.count_text(text) > 0
        assert est_an.count_text(text) > 0

    def test_for_provider_factory(self):
        est = TokenEstimator.for_provider("anthropic")
        assert isinstance(est, TokenEstimator)


# ── Format messages for summary ─────────────────────────────────────


class TestFormatMessagesForSummary:
    def test_string_content(self):
        messages = [
            Message(role="user", content="What is 2+2?"),
            Message(role="assistant", content="4"),
        ]
        result = _format_messages_for_summary(messages)
        assert "[USER] What is 2+2?" in result
        assert "[ASSISTANT] 4" in result

    def test_tool_use_block(self):
        messages = [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        tool_call_id="x1",
                        tool_name="read_file",
                        args={"path": "main.py"},
                    ),
                ],
            ),
        ]
        result = _format_messages_for_summary(messages)
        assert "[Tool: read_file" in result
        assert "main.py" in result

    def test_tool_result_block(self):
        messages = [
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id="x1",
                        result="print('hello')",
                        error=False,
                    ),
                ],
            ),
        ]
        result = _format_messages_for_summary(messages)
        assert "[Result]" in result
        assert "print('hello')" in result

    def test_tool_result_error(self):
        messages = [
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id="x1",
                        result="File not found",
                        error=True,
                    ),
                ],
            ),
        ]
        result = _format_messages_for_summary(messages)
        assert "[ERROR]" in result


# ── SummarizationMiddleware (unit, no real LLM calls) ───────────────

# Note: The _compact_messages method was removed. Compaction now only
# happens in wrap_llm_call which generates real summaries via LLM.
