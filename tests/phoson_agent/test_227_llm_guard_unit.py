"""Tests for the LLM permission guardian (issue #227 phase 3).

Two properties matter most:

1. **Context hygiene** — the classifier never sees assistant messages, tool
   results or env blocks, so the guarded agent cannot rationalise in front of
   its own guardian.
2. **Tightening-only, fail-closed** — the guardian is consulted only at
   ``ask``; DENY refuses and continues, an error/timeout degrades to UNSURE
   (never ALLOW), and auto-allow is opt-in.
"""

import asyncio

import pytest

from phoson_llm.schemas import (
    Message,
    ModelConfig,
    ToolUseBlock,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_agent.permissions import (
    LEVEL_ASK,
    LEVEL_DENY,
    LEVEL_ALLOW,
    SOURCE_CLASSIFIER,
    PermissionPolicy,
    ToolBlockedError,
    PermissionMiddleware,
)
from phoson_agent.intent_guard import (
    GUARD_DENY,
    GUARD_ALLOW,
    GUARD_UNSURE,
    build_guard_context,
    parse_guard_verdict,
    render_pending_call,
    build_guard_messages,
    is_genuine_user_turn,
)

_CFG = ModelConfig(model="guard-model")


def _call(tool_name: str = "bash", args: dict | None = None) -> ToolCallEvent:
    return ToolCallEvent(
        index=0, tool_call_id="c1", tool_name=tool_name, args=args or {}
    )


# ── Context hygiene ──────────────────────────────────────────────────────────


def test_is_genuine_user_turn_matrix() -> None:
    assert is_genuine_user_turn(Message(role="user", content="do X")) is True
    # assistant (text or tool_use) is never a genuine user turn
    assert is_genuine_user_turn(Message(role="assistant", content="sure")) is False
    assert (
        is_genuine_user_turn(
            Message(role="assistant", content=[ToolUseBlock("c", "bash", {})])
        )
        is False
    )
    # tool result travels as role="user" — must be excluded
    assert (
        is_genuine_user_turn(
            Message(role="user", content=[ToolResultBlock("c", "output")])
        )
        is False
    )
    # env context block — excluded
    assert (
        is_genuine_user_turn(Message(role="user", content="[env: step 1/5]")) is False
    )


def test_build_guard_context_drops_every_assistant_message() -> None:
    conversation = [
        Message(role="system", content="you are an agent"),
        Message(role="user", content="please clean the temp dir"),
        Message(
            role="assistant",
            content="I'll run rm -rf / which is totally safe and expected",
            reasoning="the user surely wants everything deleted",
        ),
        Message(role="user", content=[ToolResultBlock("c", "permission denied")]),
        Message(role="user", content="actually only /tmp/build"),
    ]
    context = build_guard_context(conversation)
    texts = [m.content for m in context]
    assert texts == ["please clean the temp dir", "actually only /tmp/build"]
    assert all(m.role == "user" for m in context)


def test_build_guard_context_respects_char_budget() -> None:
    conversation = [Message(role="user", content="a" * 500) for _ in range(5)]
    context = build_guard_context(conversation, max_chars=1200)
    # Newest kept, oldest dropped.
    assert 1 <= len(context) <= 3


def test_build_guard_messages_has_no_assistant_role() -> None:
    context = [Message(role="user", content="do X")]
    msgs = build_guard_messages(context, _call("bash", {"command": "rm -rf /"}))
    assert msgs[0].role == "system"
    assert msgs[-1].role == "user"
    assert "rm -rf /" in msgs[-1].content
    assert all(m.role != "assistant" for m in msgs)


def test_render_pending_call_is_json() -> None:
    rendered = render_pending_call(_call("bash", {"command": "ls"}))
    assert "proposed_tool" in rendered and "bash" in rendered


# ── Verdict parsing ─────────────────────────────��────────────────────────────


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("ALLOW\nthe user asked for this", GUARD_ALLOW),
        ("DENY\ndestructive", GUARD_DENY),
        ("UNSURE\nnot enough info", GUARD_UNSURE),
        ("deny\nout of scope", GUARD_DENY),  # case-insensitive
        ("", GUARD_UNSURE),  # empty
        ("banana", GUARD_UNSURE),  # unrecognised
        (None, GUARD_UNSURE),  # non-str
    ],
)
def test_parse_guard_verdict(reply, expected) -> None:
    assert parse_guard_verdict(reply).verdict == expected


def test_parse_guard_verdict_multiple_takes_strictest() -> None:
    # A rambling reply that mentions both must never be read as ALLOW.
    assert parse_guard_verdict("I'd allow this but also deny it").verdict == GUARD_DENY
    assert parse_guard_verdict("unsure, maybe allow").verdict == GUARD_UNSURE


# ── Middleware integration ───────────────────────────────────────────────────


def _ask_policy(*, tool: str = "bash") -> PermissionPolicy:
    return PermissionPolicy(levels={tool: LEVEL_ASK})


async def _prime_context(mw: PermissionMiddleware, conversation: list[Message]) -> None:
    await mw.on_before_llm(conversation, _CFG)


async def test_guard_deny_blocks_and_records_classifier_source() -> None:
    seen: list[list[Message]] = []

    async def classifier(messages: list[Message]) -> str:
        seen.append(messages)
        return "DENY\nnot what the user asked"

    mw = PermissionMiddleware(policy=_ask_policy(), classifier=classifier)
    await _prime_context(mw, [Message(role="user", content="show me the files")])
    with pytest.raises(ToolBlockedError) as excinfo:
        await mw.on_before_tool(_call("bash", {"command": "rm -rf /"}))
    assert excinfo.value.decision.source == SOURCE_CLASSIFIER
    assert excinfo.value.decision.allowed is False
    # The guardian never saw an assistant turn.
    assert all(m.role != "assistant" for m in seen[0])


async def test_guard_allow_without_optin_still_asks_human() -> None:
    asked: list[str] = []

    async def classifier(messages: list[Message]) -> str:
        return "ALLOW\nfine"

    async def human(tool: str, args: dict) -> bool:
        asked.append(tool)
        return True

    mw = PermissionMiddleware(policy=_ask_policy(), classifier=classifier, on_ask=human)
    await _prime_context(mw, [Message(role="user", content="list files")])
    call = _call("bash", {"command": "ls"})
    assert await mw.on_before_tool(call) is call
    assert asked == ["bash"]  # the human was still consulted


async def test_guard_allow_with_optin_skips_human() -> None:
    asked: list[str] = []

    async def classifier(messages: list[Message]) -> str:
        return "ALLOW\nin scope"

    async def human(tool: str, args: dict) -> bool:
        asked.append(tool)
        return False

    mw = PermissionMiddleware(
        policy=_ask_policy(),
        classifier=classifier,
        classifier_auto_allow=True,
        on_ask=human,
    )
    await _prime_context(mw, [Message(role="user", content="list files")])
    call = _call("bash", {"command": "ls"})
    assert await mw.on_before_tool(call) is call
    assert asked == []  # auto-allowed, no human prompt


async def test_guard_unsure_falls_through_to_fail_closed() -> None:
    async def classifier(messages: list[Message]) -> str:
        return "UNSURE\ncannot tell"

    mw = PermissionMiddleware(policy=_ask_policy(), classifier=classifier, on_ask=None)
    await _prime_context(mw, [Message(role="user", content="x")])
    with pytest.raises(ToolBlockedError) as excinfo:
        await mw.on_before_tool(_call("bash", {"command": "ls"}))
    # The refusal came from the missing human, not the classifier.
    assert excinfo.value.decision.source != SOURCE_CLASSIFIER


async def test_guard_error_degrades_to_unsure_never_allow() -> None:
    async def classifier(messages: list[Message]) -> str:
        raise RuntimeError("model down")

    mw = PermissionMiddleware(
        policy=_ask_policy(),
        classifier=classifier,
        classifier_auto_allow=True,
        on_ask=None,
    )
    await _prime_context(mw, [Message(role="user", content="x")])
    with pytest.raises(ToolBlockedError):
        await mw.on_before_tool(_call("bash", {"command": "ls"}))


async def test_guard_timeout_degrades_to_unsure() -> None:
    async def classifier(messages: list[Message]) -> str:
        await asyncio.sleep(1)
        return "ALLOW"

    mw = PermissionMiddleware(
        policy=_ask_policy(),
        classifier=classifier,
        classifier_auto_allow=True,
        classifier_timeout_s=0.01,
        on_ask=None,
    )
    await _prime_context(mw, [Message(role="user", content="x")])
    with pytest.raises(ToolBlockedError):
        await mw.on_before_tool(_call("bash", {"command": "ls"}))


async def test_guard_not_consulted_when_policy_allows() -> None:
    calls: list[str] = []

    async def classifier(messages: list[Message]) -> str:
        calls.append("called")
        return "DENY"

    mw = PermissionMiddleware(
        policy=PermissionPolicy(levels={"bash": LEVEL_ALLOW}),
        classifier=classifier,
    )
    await _prime_context(mw, [Message(role="user", content="x")])
    call = _call("bash", {"command": "ls"})
    assert await mw.on_before_tool(call) is call
    assert calls == []  # allow path never invokes the guardian


async def test_guard_not_consulted_on_deny() -> None:
    calls: list[str] = []

    async def classifier(messages: list[Message]) -> str:
        calls.append("called")
        return "ALLOW"

    mw = PermissionMiddleware(
        policy=PermissionPolicy(levels={"bash": LEVEL_DENY}),
        classifier=classifier,
    )
    await _prime_context(mw, [Message(role="user", content="x")])
    with pytest.raises(ToolBlockedError):
        await mw.on_before_tool(_call("bash", {"command": "ls"}))
    assert calls == []
