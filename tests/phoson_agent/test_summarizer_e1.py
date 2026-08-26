"""Tests for IMPROVEMENTS.md E1 — structured summaries + retained reasoning
in the summarization middleware."""

from collections.abc import AsyncIterator

import pytest

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
)
from phoson_agent.plugins.summarizer import (
    SummarizationMiddleware,
    _format_messages_for_summary,
)

# ── _format_messages_for_summary: retained reasoning ─────────────────


class TestFormatWithReasoning:
    def test_reasoning_appended_for_matching_index(self) -> None:
        msgs = [
            Message(role="user", content="do the thing"),
            Message(role="assistant", content="done"),
            Message(role="assistant", content="final"),
        ]
        out = _format_messages_for_summary(msgs, reasoning_for={1: "step one why"})
        assert "[ASSISTANT] done" in out
        assert "Reasoning:\nstep one why" in out
        # Non-matching messages stay clean.
        assert out.count("Reasoning:") == 1
        assert "final" in out

    def test_no_reasoning_when_absent(self) -> None:
        msgs = [Message(role="assistant", content="hello")]
        out = _format_messages_for_summary(msgs)
        assert "Reasoning" not in out

    def test_empty_reasoning_ignored(self) -> None:
        msgs = [Message(role="assistant", content="hello")]
        out = _format_messages_for_summary(msgs, reasoning_for={0: ""})
        assert "Reasoning" not in out

    def test_non_assistant_ignored_even_if_keyed(self) -> None:
        msgs = [Message(role="user", content="hi")]
        out = _format_messages_for_summary(msgs, reasoning_for={0: "nope"})
        assert "nope" not in out


# ── Structured summary prompt ─────────────────────────────────────────


def _mw(**kwargs) -> SummarizationMiddleware:
    kwargs.setdefault("provider", "openai")
    kwargs.setdefault("model", "gpt-4o-mini")
    return SummarizationMiddleware(**kwargs)


class TestStructuredPrompt:
    def test_default_is_structured(self) -> None:
        mw = _mw()
        assert mw.structured is True
        prompt = mw.build_summary_prompt([Message(role="user", content="hi")])
        assert "## Goal" in prompt
        assert "## Reasoning highlights" in prompt
        assert "## Next steps" in prompt
        assert "structured handoff document" in prompt
        # No reasoning was provided, so the reasoning note is omitted.
        assert "captured reasoning for some" not in prompt

    def test_reasoning_note_present_when_reasoning(self) -> None:
        mw = _mw()
        msgs = [Message(role="assistant", content="done")]
        prompt = mw.build_summary_prompt(msgs, reasoning_for={0: "the why"})
        assert "captured reasoning for some" in prompt
        assert "the why" in prompt

    def test_legacy_template_when_unstructured(self) -> None:
        mw = _mw(structured=False)
        prompt = mw.build_summary_prompt([Message(role="user", content="hi")])
        assert "## Goal" not in prompt
        assert "You are summarizing a conversation" in prompt

    def test_structured_override_per_call(self) -> None:
        mw = _mw(structured=False)
        prompt = mw.build_summary_prompt(
            [Message(role="user", content="hi")], structured=True
        )
        assert "## Goal" in prompt


# ── Retained reasoning registration + alignment ──────────────────────


class TestRetainedReasoning:
    def test_set_and_clear(self) -> None:
        mw = _mw()
        a = Message(role="assistant", content="a")
        b = Message(role="user", content="b")
        mw.set_retained_reasoning([a, b], ["why a", ""])
        assert mw._retained_by_id == {id(a): "why a"}
        mw.clear_retained_reasoning()
        assert mw._retained_by_id == {}

    def test_dict_form(self) -> None:
        mw = _mw()
        a = Message(role="assistant", content="a")
        b = Message(role="user", content="b")
        mw.set_retained_reasoning([a, b], {0: "why a"})
        assert mw._retained_by_id == {id(a): "why a"}

    def test_alignment_by_identity_across_lists(self) -> None:
        """The run's path and a fresh compaction list share Message objects.

        Reasoning registered against the original path must follow the
        messages onto their new positions.
        """
        a = Message(role="assistant", content="a")
        b = Message(role="user", content="b")
        c = Message(role="assistant", content="c")
        original = [a, b, c]
        mw = _mw()
        mw.set_retained_reasoning(original, ["reason-a", "", "reason-c"])

        # A different list containing the same objects (e.g. the engine
        # history sliced for the summary call).
        fresh = [a, b]
        prompt = mw.build_summary_prompt(fresh)
        assert "reason-a" in prompt
        assert "reason-c" not in prompt

    def test_format_for_summary_uses_retained(self) -> None:
        mw = _mw()
        a = Message(role="assistant", content="done")
        mw.set_retained_reasoning([a], ["the chain of thought"])
        out = mw.format_for_summary([a])
        assert "the chain of thought" in out

    def test_set_replaces_previous_registration(self) -> None:
        """A new run's registration fully replaces the previous one."""
        mw = _mw()
        old = Message(role="assistant", content="old")
        new = Message(role="assistant", content="new")
        mw.set_retained_reasoning([old], ["old reasoning"])
        mw.set_retained_reasoning([new], ["new reasoning"])
        assert mw._retained_by_id == {id(new): "new reasoning"}
        prompt = mw.build_summary_prompt([old, new])
        assert "new reasoning" in prompt
        assert "old reasoning" not in prompt


# ── wrap_llm_call: structured + reasoning + auto_enabled ─────────────


def _make_middleware(window: int = 1000, **kw) -> SummarizationMiddleware:
    mw = SummarizationMiddleware(
        threshold=0.50,
        min_keep_messages=2,
        provider="openai",
        model="gpt-4o-mini",
        **kw,
    )

    class _FakeResolver:
        async def resolve(self, provider: str, model: str) -> int:  # noqa: ARG002
            return window

    mw._resolver = _FakeResolver()  # type: ignore[assignment]
    return mw


@pytest.mark.asyncio
async def test_wrap_llm_call_auto_disabled_passes_through() -> None:
    """``/compact off``: the middleware must not compact, even over threshold."""
    mw = _make_middleware(window=20)
    mw.threshold = 0.05  # would normally trip immediately
    mw.auto_enabled = False

    seen: list[int] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        seen.append(id(messages))
        yield LLMDoneEvent(content="ok", has_tool_calls=False)

    msgs = [
        Message(role="user", content="old one " * 5),
        Message(role="assistant", content="old two " * 5),
        Message(role="user", content="keep me"),
    ]
    events = [
        ev async for ev in mw.wrap_llm_call(call_next, msgs, ModelConfig(model="m"))
    ]

    # Single pass-through call with the ORIGINAL messages (no summary call).
    assert seen == [id(msgs)]
    assert events and isinstance(events[0], LLMDoneEvent)
    assert mw.pop_compact_events() == []


@pytest.mark.asyncio
async def test_wrap_llm_call_structured_and_retained_reasoning() -> None:
    """Over threshold: the summary prompt is structured and carries the
    retained reasoning; the main call gets the compacted messages."""
    mw = _make_middleware(window=20)
    mw.threshold = 0.05
    mw.min_keep_messages = 1
    old_a = Message(role="assistant", content="old answer")
    mw.set_retained_reasoning(
        [Message(role="user", content="old question"), old_a],
        ["", "why we did it"],
    )

    summary_prompt: list[str] = []

    async def call_next(
        messages: list[Message], config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        content = str(messages[0].content)
        if "structured handoff document" in content:
            summary_prompt.append(content)
            yield TokenEvent(content="## Goal\ndone")
            yield LLMDoneEvent(content="## Goal\ndone", has_tool_calls=False)
        else:
            yield LLMDoneEvent(content="ok", has_tool_calls=False)

    msgs = [
        Message(role="user", content="old question"),
        old_a,
        Message(role="user", content="keep me"),
    ]
    _ = [ev async for ev in mw.wrap_llm_call(call_next, msgs, ModelConfig(model="m"))]

    assert len(summary_prompt) == 1
    prompt = summary_prompt[0]
    assert "## Reasoning highlights" in prompt
    assert "why we did it" in prompt
    assert "captured reasoning for some" in prompt


class TestBuildCompaction:
    def test_build_compaction_keeps_layout(self) -> None:
        mw = _mw(structured=False)
        msgs = [
            Message(role="system", content="sys"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="u2"),
            Message(role="assistant", content="a2"),
            Message(role="user", content="u3"),
        ]
        compacted, before, after = mw.build_compaction(msgs, "S")
        assert compacted[0].content == "sys"
        assert "[Conversation summary up to this point: S]" in str(compacted[1].content)
        # A tiny summary can cost more overhead than it saves; the point is
        # that the number is a consistent estimate of the new layout.
        assert after == mw.estimate_tokens(compacted)
        # Default min_keep_messages=4: summary + last four kept.
        assert len(compacted) == 1 + 1 + 4
