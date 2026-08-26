"""Tests for summarization middleware and token estimator."""

from collections.abc import AsyncIterator

import pytest

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolResultBlock,
)
from phoson_agent.plugins.summarizer import (
    TokenEstimator,
    SummarizationMiddleware,
    _format_messages_for_summary,
)

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


# ── wrap_llm_call: integration without real LLM ─────────────────────


class _FakeResolver:
    """Stand-in for ContextWindowResolver — returns a fixed context window."""

    def __init__(self, window: int) -> None:
        self._window = window

    async def resolve(self, provider: str, model: str) -> int:  # noqa: ARG002
        return self._window


def _make_middleware(window: int = 1000) -> SummarizationMiddleware:
    mw = SummarizationMiddleware(
        threshold=0.50,
        min_keep_messages=2,
        provider="openai",
        model="gpt-4o-mini",
    )
    mw._resolver = _FakeResolver(window)  # type: ignore[assignment]
    return mw


@pytest.mark.asyncio
async def test_wrap_llm_call_passthrough_when_below_threshold() -> None:
    mw = _make_middleware(window=10_000)

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield TokenEvent(content="hi")
        yield LLMDoneEvent(content="hi", has_tool_calls=False)

    msgs = [Message(role="user", content="short")]
    cfg = ModelConfig(model="gpt-4o-mini")
    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    types = [type(e).__name__ for e in events]
    assert types == ["LLMStartEvent", "TokenEvent", "LLMDoneEvent"]
    assert mw.pop_compact_events() == []


@pytest.mark.asyncio
async def test_wrap_llm_call_forwards_summary_usage_event() -> None:
    """The UsageEvent of the internal summary call must be forwarded.

    Otherwise the cost of summarization is silently lost from
    ``AgentRunResult.total_cost_usd``.
    """
    # Force the threshold to trip with very few tokens.
    mw = _make_middleware(window=20)
    mw.threshold = 0.10  # ~2 tokens
    mw.min_keep_messages = 1

    summary_usage = UsageEvent(
        model="gpt-4o-mini",
        usage=TokenUsage(input=50, output=10),
        cost_usd=0.000123,
        cost_known=True,
    )
    main_usage = UsageEvent(
        model="gpt-4o-mini",
        usage=TokenUsage(input=5, output=2),
        cost_usd=0.000045,
        cost_known=True,
    )

    call_counter = {"n": 0}

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            # Summary call — emits a TokenEvent (consumed) and a UsageEvent
            # (must be forwarded).
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield TokenEvent(content="brief summary")
            yield summary_usage
            yield LLMDoneEvent(content="brief summary", has_tool_calls=False)
        else:
            # Main call — every event must be forwarded.
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield TokenEvent(content="ok")
            yield main_usage
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    msgs = [
        Message(role="system", content="be helpful"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="user", content="follow up"),
        Message(role="assistant", content="sure"),
        Message(role="user", content="another"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    # Both the summary's usage AND the main call's usage must reach the caller.
    assert summary_usage in usage_events
    assert main_usage in usage_events

    # Visual events of the summary call must NOT leak through (otherwise the
    # consumer would see two LLMStart/LLMDone events for a single user turn).
    start_events = [e for e in events if isinstance(e, LLMStartEvent)]
    done_events = [e for e in events if isinstance(e, LLMDoneEvent)]
    assert len(start_events) == 1
    assert len(done_events) == 1

    # And a SummarizationEvent should be queued for the agent.
    pending = mw.pop_compact_events()
    assert len(pending) == 1
    assert pending[0].messages_removed > 0


@pytest.mark.asyncio
async def test_wrap_llm_call_generates_real_summary_via_llm() -> None:
    """The compacted message must carry the LLM-generated summary, the system
    prompt must be preserved intact, and the most recent messages must be
    passed through unchanged to the second call."""
    mw = _make_middleware(window=20)
    mw.threshold = 0.10
    mw.min_keep_messages = 2

    captured: dict[str, list[Message]] = {}

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        if "structured handoff document" in str(messages[0].content):
            # Internal summary call — mock LLM produces the summary text.
            captured["summary_call"] = list(messages)
            yield TokenEvent(content="USER WANTS X. DECIDED Y.")
            yield LLMDoneEvent(content="USER WANTS X. DECIDED Y.", has_tool_calls=False)
        else:
            # Main call with the compacted messages.
            captured["main_call"] = list(messages)
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    msgs = [
        Message(role="system", content="be helpful"),
        Message(role="user", content="old question one"),
        Message(role="assistant", content="old answer one"),
        Message(role="user", content="old question two"),
        Message(role="assistant", content="old answer two"),
        Message(role="user", content="recent question"),
        Message(role="assistant", content="recent answer"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    _ = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    assert "summary_call" in captured and "main_call" in captured
    main_msgs = captured["main_call"]

    # 1) Exactly one summary message, containing the generated text.
    summary_msgs = [m for m in main_msgs if "Conversation summary" in str(m.content)]
    assert len(summary_msgs) == 1
    assert "USER WANTS X. DECIDED Y." in str(summary_msgs[0].content)

    # 2) System prompt preserved intact and first.
    assert main_msgs[0].role == "system"
    assert main_msgs[0].content == "be helpful"

    # 3) The most recent min_keep_messages are kept verbatim at the tail.
    assert str(main_msgs[-2].content) == "recent question"
    assert str(main_msgs[-1].content) == "recent answer"

    # 4) Summarized (old) messages are gone.
    assert all("old question" not in str(m.content) for m in main_msgs)


@pytest.mark.asyncio
async def test_wrap_llm_call_summary_does_not_leak_prompt() -> None:
    """The compacted message must contain only the generated summary — never
    the full summary prompt (instructions + formatted history)."""
    mw = _make_middleware(window=20)
    mw.threshold = 0.10
    mw.min_keep_messages = 1

    calls: list[list[Message]] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        calls.append(list(messages))
        if "structured handoff document" in str(messages[0].content):
            yield TokenEvent(content="tiny summary")
            yield LLMDoneEvent(content="tiny summary", has_tool_calls=False)
        else:
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    msgs = [
        Message(role="user", content="old one " * 5),
        Message(role="assistant", content="old two " * 5),
        Message(role="user", content="keep me"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    _ = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    assert len(calls) == 2
    main_msgs = calls[1]
    for m in main_msgs:
        assert "You are summarizing a conversation" not in str(m.content)
        assert "Instructions:" not in str(m.content)
    summary_msgs = [m for m in main_msgs if "Conversation summary" in str(m.content)]
    assert len(summary_msgs) == 1
    assert str(summary_msgs[0].content) == (
        "[Conversation summary up to this point: tiny summary]"
    )


@pytest.mark.asyncio
async def test_on_before_llm_does_not_mutate_messages() -> None:
    """No double compaction: `on_before_llm` must return the messages
    untouched — real compaction only happens in `wrap_llm_call`."""
    mw = _make_middleware(window=20)
    mw.threshold = 0.10

    msgs = [
        Message(role="user", content="old one"),
        Message(role="assistant", content="old two"),
        Message(role="user", content="new"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    out = await mw.on_before_llm(msgs, cfg)
    assert out is msgs
    assert len(out) == 3
    assert out[0].content == "old one"


def test_summarization_middleware_resolver_initialized() -> None:
    """Regression: ``_resolver`` must be initialized after ``__post_init__``.

    The previous implementation typed it as ``Optional`` with a ``# type: ignore``
    default of ``None``. Now it's ``init=False`` and always set.
    """
    mw = SummarizationMiddleware(
        threshold=0.80,
        min_keep_messages=4,
        provider="openai",
        model="gpt-4o-mini",
    )

    assert mw._resolver is not None
    assert mw._estimator is not None
