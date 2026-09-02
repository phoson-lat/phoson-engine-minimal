"""Regression tests for #176 (F-10, F-11): compaction must not break tool pairs.

- ``safe_cut_index`` backs a tool-pair boundary up so the kept tail never
  starts on an orphaned ``tool_result``.
- An empty summary result aborts the compaction (no silent history loss).
- The internal summary call is tool-free when a chat client is injected.
- A tool-pairing 400 surfaces as an explicit, diagnosable error instead of
  being swallowed as a context-length rescue.
"""

import pytest

from phoson_llm.utils import (
    TOOL_PAIRING_ERROR_CODE,
    is_tool_pairing_error,
)
from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    ToolResultBlock,
)
from phoson_agent.plugins.summarizer import (
    SummarizationMiddleware,
    safe_cut_index,
    _has_tool_result,
)

# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeResolver:
    def __init__(self, window: int) -> None:
        self._window = window

    async def resolve(self, provider: str, model: str) -> int:  # noqa: ARG002
        return self._window


def _mw(window: int = 1000) -> SummarizationMiddleware:
    mw = SummarizationMiddleware(
        threshold=0.50,
        min_keep_messages=2,
        provider="openai",
        model="gpt-4o-mini",
    )
    mw._resolver = _FakeResolver(window)  # type: ignore[assignment]
    return mw


def _tool_use(i: int, name: str = "bash") -> Message:
    return Message(
        role="assistant",
        content=[ToolUseBlock(tool_call_id=f"t{i}", tool_name=name, args={})],
    )


def _tool_result(i: int, text: str = "ok") -> Message:
    return Message(
        role="user",
        content=[ToolResultBlock(tool_call_id=f"t{i}", result=text)],
    )


def _tool_call_pair(i: int) -> list[Message]:
    """An assistant tool_use + its user tool_result (one tool call)."""
    return [_tool_use(i), _tool_result(i)]


# ── safe_cut_index ───────────────────────────────────────────────────────────


class TestSafeCutIndex:
    def test_backs_up_over_orphaned_tool_result(self) -> None:
        """A cut landing on a user tool_result pulls the tool_use into the
        kept tail (the F-10 bug)."""
        # history: sys-less, 5 messages, min_keep=2.
        # naive cut index = 3 → others[3] is the tool_result of t1.
        others = [
            Message(role="user", content="u0"),
            Message(role="assistant", content="a0"),
            *[_tool_use(1), _tool_result(1)],
            Message(role="assistant", content="a1"),
        ]
        naive = len(others) - 2  # == 3 → an orphaned tool_result
        assert _has_tool_result(others[naive])

        cut = safe_cut_index(others, 2)
        # Cut must land before the tool_result (on the assistant tool_use)
        # so the whole pair is kept together.
        assert not _has_tool_result(others[cut])
        assert others[cut].role == "assistant"
        assert cut == 2

    def test_no_change_for_plain_text(self) -> None:
        """A cut that already lands on a non-tool_result keeps the naive cut."""
        others = [
            Message(role="user", content="u0"),
            Message(role="assistant", content="a0"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a1"),
        ]
        assert safe_cut_index(others, 2) == 2

    def test_floor_at_zero(self) -> None:
        """Even when the whole history is one tool pair, the cut floors at 0
        (nothing to summarize) rather than going negative."""
        others = [_tool_use(0), _tool_result(0)]
        assert safe_cut_index(others, 2) == 0
        # The safe cut swallowed everything → the caller treats this as
        # "nothing to summarize".
        assert safe_cut_index(others, 1) == 0


class TestHasToolResult:
    def test_detects_tool_result(self) -> None:
        assert _has_tool_result(_tool_result(0))

    def test_ignores_text_user(self) -> None:
        assert not _has_tool_result(Message(role="user", content="hi"))

    def test_ignores_assistant_tool_use(self) -> None:
        assert not _has_tool_result(_tool_use(0))

    def test_ignores_image_user(self) -> None:
        from phoson_llm.schemas import ImageBlock

        assert not _has_tool_result(
            Message(role="user", content=[ImageBlock(source="file://x.png")])
        )


# ── _compact: safe cut + abort on empty summary ──────────────────────────────


@pytest.mark.asyncio
async def test_compact_keeps_tool_pair_together() -> None:
    """Auto-compact must not leave the kept tail starting on an orphaned
    tool_result (the F-10 400). The tool_use + tool_result both survive."""
    mw = _mw(window=1000)
    mw.structured = False
    # min_keep=2 → naive cut lands on the tool_result; safe cut backs up.
    mw.min_keep_messages = 2
    others = [
        Message(role="user", content="u0"),
        Message(role="assistant", content="a0"),
        *_tool_call_pair(1),
        Message(role="assistant", content="a1"),
    ]
    # Pad so the gate fires (request > trigger).
    big = "x " * 260
    others[0].content = big
    others[1].content = big

    def call_next(messages, config):  # noqa: ARG001
        async def gen():
            # Only the main call reaches the chain; the summary round trip
            # is tool-free via `chat` when injected (not exercised here).
            yield TokenEvent(content="ok")
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

        return gen()

    before = len(others)
    _ = [
        ev
        async for ev in mw.wrap_llm_call(
            call_next, others, ModelConfig(model="m", max_tokens=400)
        )
    ]

    compacted = others  # spliced in place
    assert len(compacted) < before, "compaction did not run"
    # Every tool_result in the compacted history has its tool_use kept.
    tool_result_msgs = [
        m for m in compacted if not isinstance(m.content, str) and _has_tool_result(m)
    ]
    assert tool_result_msgs, "expected a tool_result to survive (kept tail)"
    for m in tool_result_msgs:
        # find the matching tool_use earlier in the compacted list
        tid = next(b.tool_call_id for b in m.content if isinstance(b, ToolResultBlock))
        assert any(
            not isinstance(x.content, str)
            and any(
                isinstance(b, ToolUseBlock) and b.tool_call_id == tid for b in x.content
            )
            for x in compacted
        ), f"tool_use for {tid} was dropped but its result kept"


@pytest.mark.asyncio
async def test_compact_aborts_on_empty_summary() -> None:
    """F-11: an empty summary result must NOT splice the history. The
    original messages pass through untouched."""
    mw = _mw(window=1000)
    mw.structured = False
    mw.min_keep_messages = 2
    msgs = [
        Message(role="user", content="x " * 260),
        Message(role="assistant", content="x " * 260),
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
    ]
    snapshot = list(msgs)
    captured: dict[str, list[Message]] = {}

    def call_next(messages, config):  # noqa: ARG001
        async def gen():
            if "summarizing" in str(messages[0].content).lower():
                captured["summary"] = list(messages)
                # The model answered with nothing (e.g. a tool call) — no text.
                yield LLMDoneEvent(content="", has_tool_calls=True)
            else:
                captured["main"] = list(messages)
                yield TokenEvent(content="ok")
                yield LLMDoneEvent(content="ok", has_tool_calls=False)

        return gen()

    _ = [
        ev
        async for ev in mw.wrap_llm_call(
            call_next, msgs, ModelConfig(model="m", max_tokens=400)
        )
    ]

    # Compaction was attempted (summary call made) but aborted: the main
    # call saw the ORIGINAL history, and no compaction event was queued.
    assert "summary" in captured
    assert "main" in captured
    assert len(captured["main"]) == len(snapshot), (
        "history was spliced despite empty summary"
    )
    assert mw.pop_compact_events() == [], (
        "an empty-summary compaction must not be recorded"
    )


@pytest.mark.asyncio
async def test_summary_call_is_tool_free_when_chat_injected() -> None:
    """F-11 root cause: with a chat client injected, the internal summary
    round trip must not send the run's tool schemas."""
    mw = _mw(window=1000)
    mw.structured = False
    mw.min_keep_messages = 2
    msgs = [
        Message(role="user", content="x " * 260),
        Message(role="assistant", content="x " * 260),
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
    ]

    seen: dict[str, object] = {}

    class _FakeChat:
        async def stream(self, messages, config, tools=None):  # noqa: ANN001
            seen["tools"] = tools
            seen["config"] = config
            seen["messages"] = list(messages)
            yield TokenEvent(content="SUMMARY")
            yield LLMDoneEvent(content="SUMMARY", has_tool_calls=False)

    mw.chat = _FakeChat()

    main_seen = []

    def call_next(messages, config):  # noqa: ARG001
        async def gen():
            # The MAIN call uses the chain (call_next). If the *summary*
            # call wrongly came through here (it carries max_tokens=4096),
            # fail loudly — that path hands the model the run's tools.
            if config.max_tokens == 4096:
                raise AssertionError("summary round trip used call_next (has tools)")
            main_seen.append(list(messages))
            yield TokenEvent(content="ok")
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

        return gen()

    _ = [
        ev
        async for ev in mw.wrap_llm_call(
            call_next, msgs, ModelConfig(model="m", max_tokens=400)
        )
    ]

    # The summary call went through the tool-free chat client (no schemas),
    # and the main call went through the chain as before.
    assert "tools" in seen, "the summary round trip never reached the chat client"
    assert seen["tools"] is None, "summary call must be tool-free (tools=None)"
    assert seen["config"].max_tokens == 4096, "chat client saw the summary call"
    assert main_seen, "main call never reached the chain"


# ── rescue: explicit error on a tool-pairing 400 ─────────────────────────────


def _pairing_error() -> ErrorEvent:
    return ErrorEvent(
        message="messages.3: tool_result without tool_use: no matching tool_use",
        code="unknown",
        retryable=False,
    )


@pytest.mark.asyncio
async def test_rescue_surfaces_pairing_400_explicitly() -> None:
    """A pre-commit 400 that is a tool-pairing mismatch (not a context
    error) must surface as an explicit, diagnosable error — not be
    swallowed as a context-length rescue and silently re-compacted."""
    mw = _mw(window=1000)
    mw.structured = False
    mw.min_keep_messages = 2
    msgs = [
        Message(role="user", content="x " * 260),
        Message(role="assistant", content="x " * 260),
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
    ]

    def call_next(messages, config):  # noqa: ARG001
        async def gen():
            yield _pairing_error()

        return gen()

    events = [
        ev
        async for ev in mw.wrap_llm_call(
            call_next, msgs, ModelConfig(model="m", max_tokens=400)
        )
    ]

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors, "expected an error to surface"
    assert errors[-1].code == TOOL_PAIRING_ERROR_CODE
    assert "malformed" in errors[-1].message.lower()
    # The rescue must NOT have compacted in place (the history is intact).
    assert len(msgs) == 4


# ── is_tool_pairing_error ────────────────────────────────────────────────────


class TestIsToolPairingError:
    @pytest.mark.parametrize(
        "msg",
        [
            "messages.3: tool_result without tool_use: no matching tool_use",
            "tool_result does not correspond to a tool_use in the same message",
            "Invalid request: orphan tool_result found",
        ],
    )
    def test_detects_pairing(self, msg: str) -> None:
        assert is_tool_pairing_error(msg)

    @pytest.mark.parametrize(
        "msg",
        [
            "prompt is too long: 199999 tokens > 198000 maximum",
            "This model's maximum context length is 8192 tokens",
            "Invalid tool schema for 'bash': missing 'command'",
        ],
    )
    def test_rejects_non_pairing(self, msg: str) -> None:
        assert not is_tool_pairing_error(msg)
