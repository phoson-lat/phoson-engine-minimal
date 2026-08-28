"""I-91: conservative auto-compact gate + emergency 400 rescue.

Acceptance criteria from IMPROVEMENTS.md:

- A simulated context-limit setup fires the auto-compact *before* the
  request reaches 100% of the window (the gate must reserve
  ``max_tokens`` + a safety margin, not just watch ``threshold``).
- A mock provider answering 400 "context length exceeded" triggers an
  emergency compaction and the session continues (one retry, no loops).
"""

from collections.abc import AsyncIterator

import pytest

from phoson_llm.utils import CONTEXT_LENGTH_ERROR_CODE
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    AudioBlock,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    LLMDoneEvent,
    DocumentBlock,
    ToolDefinition,
)
from phoson_agent.plugins.summarizer import (
    TokenEstimator,
    SummarizationMiddleware,
)


class _FakeResolver:
    """Fixed context window + records ``override()`` calls (I-91)."""

    def __init__(self, window: int) -> None:
        self._window = window
        self.overrides: dict[str, int] = {}

    async def resolve(self, provider: str, model: str) -> int:  # noqa: ARG002
        return self._window

    def override(self, provider: str, model: str, context_window: int) -> None:
        self.overrides[f"{provider}/{model}"] = context_window


def _make_middleware(
    window: int = 1000,
) -> tuple[SummarizationMiddleware, _FakeResolver]:
    mw = SummarizationMiddleware(
        threshold=0.80,
        min_keep_messages=2,
        provider="openai",
        model="gpt-4o-mini",
    )
    resolver = _FakeResolver(window)
    mw._resolver = resolver  # type: ignore[assignment]
    return mw, resolver


async def _ok_stream(text: str = "ok") -> AsyncIterator[LLMEvent]:
    yield TokenEvent(content=text)
    yield LLMDoneEvent(content=text, has_tool_calls=False)


async def _summary_stream(text: str = "SUMMARY") -> AsyncIterator[LLMEvent]:
    yield TokenEvent(content=text)
    yield LLMDoneEvent(content=text, has_tool_calls=False)


def _context_error(message: str = "prompt is too long") -> ErrorEvent:
    return ErrorEvent(message=message, code=CONTEXT_LENGTH_ERROR_CODE, retryable=False)


# ── Gate: conservative estimate ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_fires_before_100_when_output_reserved() -> None:
    """The gate must fire before the input alone reaches 100% of the
    window: with window=1000, max_tokens=400 the trigger is
    min(0.8*1000, 1000-400-100) = 500 — the OLD gate (0.8*1000=800)
    would have let a 550-token request through and died on the 400."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = False

    # ~550 tokens of history across 4 messages.
    msgs = [
        Message(role="user", content="word " * 140),
        Message(role="assistant", content="word " * 140),
        Message(role="user", content="word " * 140),
        Message(role="assistant", content="word " * 140),
    ]
    cfg = ModelConfig(model="gpt-4o-mini", max_tokens=400)

    est = mw._estimator.estimate_request(msgs)
    assert est > 500  # above the new trigger
    assert est <= 800  # but below the OLD threshold — the bug scenario

    main_seen: list[list[Message]] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        if "summarizing" in str(messages[0].content).lower():
            async for e in _summary_stream("S"):
                yield e
        else:
            main_seen.append(list(messages))
            yield TokenEvent(content="ok")
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    before = len(msgs)
    _ = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    # Compaction fired and the main call saw the compacted list.
    assert mw.pop_compact_events(), "auto-compact did not fire"
    assert main_seen, "main call never happened"
    assert len(main_seen[0]) < before
    # The in-place splice shrank the caller's list too.
    assert len(msgs) < before


@pytest.mark.asyncio
async def test_gate_counts_system_prompt_and_tools() -> None:
    """System prompt and tool schemas are part of every request — the
    gate must count them even when the message history is small."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = False
    mw.tool_definitions = [
        ToolDefinition(
            name="bash",
            description=("Run a shell command. " + "x " * 80),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "cmd " * 40},
                },
            },
        ),
        ToolDefinition(
            name="read_file",
            description=("Read a file. " + "y " * 80),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "path " * 40},
                },
            },
        ),
    ]

    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="user", content="hi again"),
        Message(role="assistant", content="hey"),
    ]
    # ~600 tokens of system prompt pushes the request past the trigger
    # even though the message history is only ~20 tokens.
    cfg = ModelConfig(
        model="gpt-4o-mini",
        max_tokens=400,
        system="system " * 150,
    )

    assert (
        mw._estimator.estimate_request(
            msgs, system=cfg.system, tools=mw.tool_definitions
        )
        > 500
    )

    compacted_main: list[list[Message]] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        if "summarizing" in str(messages[0].content).lower():
            async for e in _summary_stream("S"):
                yield e
        else:
            compacted_main.append(list(messages))
            yield TokenEvent(content="ok")
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    before = len(msgs)
    _ = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    assert mw.pop_compact_events(), "gate ignored system+tools weight"
    assert len(compacted_main[0]) < before


def test_multimodal_blocks_are_counted() -> None:
    """I-91: media blocks used to be skipped (4-token overhead only);
    now they carry conservative flat estimates."""
    est = TokenEstimator(provider="openai")
    text_only = [Message(role="user", content="look at this")]
    base = est.count_messages(text_only)

    for block, floor in [
        (ImageBlock(source="file://x.png"), 1000),
        (ImageBlock(source="file://x.png", detail="low"), 1000),
        (AudioBlock(source="file://a.wav"), 1000),
        (VideoBlock(source="file://v.mp4"), 5000),
        (DocumentBlock(source="file://d.pdf", pages=10), 200),
        (DocumentBlock(source="file://d.pdf"), 500),
    ]:
        with_media = [Message(role="user", content=[block])]
        assert est.count_messages(with_media) >= floor, type(block).__name__
        assert est.count_messages(with_media) > base

    # estimate_request >= count_messages always holds.
    assert est.estimate_request(text_only, system="s") >= base


# ── Emergency rescue on 400 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_400_compacts_and_retries() -> None:
    """Mock provider 400 → emergency compaction → one retry → success.
    The original 400 must never reach the user."""
    mw, resolver = _make_middleware(window=1000)
    mw.structured = False  # short legacy prompt so the summary fits

    # Small history: the gate does NOT fire (request < 500); only the
    # provider's 400 triggers the rescue.
    msgs = [
        Message(role="system", content="s"),
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
        Message(role="assistant", content="four"),
        Message(role="user", content="five"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    calls: list[list[Message]] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        calls.append(list(messages))
        if len(calls) == 1:
            yield _context_error("This model's maximum context length is 1000 tokens")
            return
        if "summarizing" in str(messages[0].content).lower():
            yield TokenEvent(content="SUM")
            yield LLMDoneEvent(content="SUM", has_tool_calls=False)
            return
        yield TokenEvent(content="ok")
        yield LLMDoneEvent(content="ok", has_tool_calls=False)

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    # 1) The 400 was swallowed — no ErrorEvent reached the caller.
    assert not any(isinstance(e, ErrorEvent) for e in events)
    # 2) The retry happened and its output was forwarded.
    assert any(isinstance(e, LLMDoneEvent) for e in events)
    assert len(calls) == 3  # failed main + summary + retry
    # 3) The history was compacted in place (summary notice + tail).
    assert len(msgs) < 6
    assert any("Emergency compaction" in str(m.content) for m in msgs)
    assert any("SUM" in str(m.content) for m in msgs)
    # 4) A SummarizationEvent was queued for the front end.
    pending = mw.pop_compact_events()
    assert len(pending) == 1
    assert pending[0].messages_removed > 0
    # 5) The real window was learned from the error message.
    assert resolver.overrides.get("openai/gpt-4o-mini") == 1000


@pytest.mark.asyncio
async def test_rescue_gives_up_after_second_400() -> None:
    """If the request is still too long after compaction, the error
    propagates — exactly one retry, no compaction loop."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = False

    # Only the recent tail remains (len(others) == min_keep) — nothing
    # left to summarize, so the rescue cannot shrink the request and
    # must fail fast without a retry.
    msgs = [
        Message(role="user", content="a"),
        Message(role="assistant", content="b"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    calls = 0

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        nonlocal calls
        calls += 1
        yield _context_error("prompt is too long")

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    # Nothing to compact → the 400 propagates without a retry.
    assert calls == 1
    assert events and isinstance(events[-1], ErrorEvent)
    assert events[-1].code == CONTEXT_LENGTH_ERROR_CODE


@pytest.mark.asyncio
async def test_rescue_no_retry_loop_when_still_too_long() -> None:
    """Compaction ran but the retry still 400s → the second error
    propagates and the total main-call count stays at two."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = False

    msgs = [
        Message(role="system", content="s"),
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
        Message(role="assistant", content="four"),
        Message(role="user", content="five"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    calls = 0

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        nonlocal calls
        calls += 1
        if "summarizing" in str(messages[0].content).lower():
            yield TokenEvent(content="SUM")
            yield LLMDoneEvent(content="SUM", has_tool_calls=False)
            return
        yield _context_error("prompt is too long")

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    # 1 failed main + 1 summary + 1 failed retry = 3 calls, no more.
    assert calls == 3
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == CONTEXT_LENGTH_ERROR_CODE


@pytest.mark.asyncio
async def test_no_rescue_after_visible_output() -> None:
    """Once tokens are flowing the response is committed — a late 400
    must be forwarded as-is, never retried (duplicate output)."""
    mw, _ = _make_middleware(window=1000)

    msgs = [
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    calls = 0

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        nonlocal calls
        calls += 1
        yield TokenEvent(content="partial")
        yield _context_error("prompt is too long")

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    assert calls == 1
    assert any(isinstance(e, TokenEvent) for e in events)
    assert events[-1] is not None and isinstance(events[-1], ErrorEvent)
    assert mw.pop_compact_events() == []


@pytest.mark.asyncio
async def test_rescue_works_even_when_auto_compact_disabled() -> None:
    """``/compact off`` disables *proactive* compaction, but the 400
    rescue is error recovery — it must still fire."""
    mw, _ = _make_middleware(window=1000)
    mw.auto_enabled = False
    mw.structured = False

    msgs = [
        Message(role="system", content="s"),
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
        Message(role="assistant", content="four"),
        Message(role="user", content="five"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    calls = 0

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _context_error("prompt is too long")
            return
        if "summarizing" in str(messages[0].content).lower():
            yield TokenEvent(content="SUM")
            yield LLMDoneEvent(content="SUM", has_tool_calls=False)
            return
        yield TokenEvent(content="ok")
        yield LLMDoneEvent(content="ok", has_tool_calls=False)

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    assert calls == 3
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any("Emergency compaction" in str(m.content) for m in msgs)


@pytest.mark.asyncio
async def test_rescue_hard_truncation_when_summary_fails() -> None:
    """If the summary call itself fails (e.g. the prompt still doesn't
    fit), the rescue falls back to a hard truncation — recent tail +
    notice — instead of losing the turn."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = True  # long template → prompt won't fit the budget

    msgs = [
        Message(role="system", content="s"),
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
        Message(role="assistant", content="four"),
        Message(role="user", content="five"),
    ]
    cfg = ModelConfig(model="gpt-4o-mini")
    calls = 0

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _context_error("prompt is too long")
            return
        if "Summarize the conversation segment" in str(messages[0].content):
            # The summary call itself 400s.
            yield _context_error("prompt is too long")
            return
        yield TokenEvent(content="ok")
        yield LLMDoneEvent(content="ok", has_tool_calls=False)

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    # The turn survives via hard truncation.
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(isinstance(e, LLMDoneEvent) for e in events)
    assert len(msgs) < 6
    assert any("dropped" in str(m.content) for m in msgs)
    assert mw.pop_compact_events()


@pytest.mark.asyncio
async def test_gate_splices_in_place() -> None:
    """The compacted list must replace the caller's list *in place* —
    the engine's history is the same object, so the rest of the run
    sees the compacted history (no re-compaction per iteration)."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = False

    msgs = [
        Message(role="user", content="word " * 140),
        Message(role="assistant", content="word " * 140),
        Message(role="user", content="word " * 140),
        Message(role="assistant", content="word " * 140),
    ]
    cfg = ModelConfig(model="gpt-4o-mini", max_tokens=400)
    original_id = id(msgs)
    main_calls: list[list[Message]] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        if "summarizing" in str(messages[0].content).lower():
            async for e in _summary_stream("S"):
                yield e
        else:
            main_calls.append(messages)  # keep the reference, not a copy
            yield TokenEvent(content="ok")
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    _ = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]

    assert main_calls
    assert id(main_calls[0]) == original_id  # same list object
    assert len(main_calls[0]) < 4


@pytest.mark.asyncio
async def test_summary_usage_event_still_forwarded() -> None:
    """Regression: the internal summary call's UsageEvent must reach
    the caller (compaction cost is not free)."""
    mw, _ = _make_middleware(window=1000)
    mw.structured = False

    msgs = [
        Message(role="user", content="word " * 140),
        Message(role="assistant", content="word " * 140),
        Message(role="user", content="word " * 140),
        Message(role="assistant", content="word " * 140),
    ]
    cfg = ModelConfig(model="gpt-4o-mini", max_tokens=400)
    summary_usage = UsageEvent(
        model="gpt-4o-mini",
        usage=TokenUsage(input=50, output=10),
        cost_usd=0.000123,
        cost_known=True,
    )

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        if "summarizing" in str(messages[0].content).lower():
            yield TokenEvent(content="S")
            yield summary_usage
            yield LLMDoneEvent(content="S", has_tool_calls=False)
            return
        yield TokenEvent(content="ok")
        yield LLMDoneEvent(content="ok", has_tool_calls=False)

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]
    assert summary_usage in [e for e in events if isinstance(e, UsageEvent)]


@pytest.mark.asyncio
async def test_gate_below_threshold_is_passthrough() -> None:
    """No compaction, no rescue: a small request passes through
    untouched and no extra LLM calls are made."""
    mw, _ = _make_middleware(window=1000)

    msgs = [Message(role="user", content="hi")]
    cfg = ModelConfig(model="gpt-4o-mini", max_tokens=400)
    calls = 0

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        nonlocal calls
        calls += 1
        yield TokenEvent(content="ok")
        yield LLMDoneEvent(content="ok", has_tool_calls=False)

    events = [ev async for ev in mw.wrap_llm_call(call_next, msgs, cfg)]
    assert calls == 1
    assert [type(e).__name__ for e in events] == ["TokenEvent", "LLMDoneEvent"]
    assert mw.pop_compact_events() == []
