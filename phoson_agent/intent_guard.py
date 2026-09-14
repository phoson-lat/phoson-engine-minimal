"""LLM permission guardian: context hygiene + verdict handling (issue #227).

Phase 3 of #144/#227 asks for a *classifier* that acts as the agent's
guardian. The one hard requirement spelled out in the issue is about what the
guardian is allowed to see:

    "Quitar los mensajes del assistant del contexto del clasificador, para que
    no pueda racionalizar ante su propio guardián."

That is the whole point of this module. The guardian must judge an **action**
against the **user's actual intent** — never against the assistant's own
justifications, which are exactly the rationalisation it would use to talk its
guardian into approving a harmful call. So the context builder here keeps only
*genuine user turns* and drops:

* every ``assistant`` message (text **and** reasoning),
* every tool result (``role="user"`` carrying a ``ToolResultBlock`` — tool
  output is untrusted data, so it must never steer the guardian),
* the environmental-context blocks (``[env: …]``), which are host artifacts,
  not user intent.

Design contract — the guardian is a *tightening* layer:

* it is consulted **only when the deterministic gate resolved to** ``ask``
  (the point where the policy is already unsure and would bother a human);
* ``DENY`` refuses the call and the run continues;
* ``ALLOW`` may skip the human prompt **only** when the host opted in
  (``classifier_auto_allow``), otherwise it still asks;
* ``UNSURE``, an unparseable reply, a timeout or any classifier error
  degrades to the deterministic behaviour (human, or fail-closed when there is
  no interactive callback).

Nothing here calls a model: the actual classifier is a host-injected async
callable (``GuardClassifier``), which keeps this layer pure and testable with
a fake, exactly like ``AskCallback``.
"""

import json
import logging
from typing import Any
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from phoson_llm.schemas import (
    Message,
    TextBlock,
    ToolCallEvent,
    ToolResultBlock,
)

_LOGGER = logging.getLogger(__name__)

#: Guardian verdicts. ``UNSURE`` is the safe default for anything ambiguous.
GUARD_ALLOW = "allow"
GUARD_DENY = "deny"
GUARD_UNSURE = "unsure"
VALID_GUARD_VERDICTS = frozenset({GUARD_ALLOW, GUARD_DENY, GUARD_UNSURE})

#: Strictness order used when a reply mentions several verdicts (the strictest
#: wins, so a rambling "I'd allow this, but deny…" can never be read as ALLOW).
_VERDICT_STRICTNESS: dict[str, int] = {GUARD_ALLOW: 0, GUARD_UNSURE: 1, GUARD_DENY: 2}

#: Host-injected classifier: given the *already hygiene-filtered* message list
#: (system guard prompt + user turns + the pending call), return the raw reply.
#: The message list is built by the middleware, so a host cannot accidentally
#: leak assistant messages into the guardian's context.
GuardClassifier = Callable[[list[Message]], Awaitable[str]]

#: Env-context prefix (kept in sync with ``middleware.ENV_CONTEXT_PREFIX``;
#: duplicated here to avoid importing the middleware module for one constant).
_ENV_CONTEXT_PREFIX = "[env: "

#: Default system prompt for the guardian. Deliberately terse and explicit
#: about the output contract; the host may override it.
GUARD_SYSTEM_PROMPT = (
    "You are a security guardian for an autonomous coding agent. You decide "
    "whether ONE proposed tool call is safe given the user's original request. "
    "You never see the agent's own reasoning or justifications — only the "
    "user's words and the concrete action — so you must judge the action on "
    "its own merits.\n\n"
    "Reply with exactly one word on the first line:\n"
    "  ALLOW  — the action clearly serves the user's request and is safe;\n"
    "  DENY   — the action is outside the user's request, destructive, "
    "exfiltrating, or otherwise unsafe;\n"
    "  UNSURE — you cannot tell.\n"
    "Follow it with a one-line reason. When in doubt, answer UNSURE — never "
    "ALLOW."
)


@dataclass(frozen=True)
class GuardDecision:
    """Parsed verdict of one classifier reply."""

    verdict: str
    rationale: str = ""

    @property
    def is_allow(self) -> bool:
        return self.verdict == GUARD_ALLOW

    @property
    def is_deny(self) -> bool:
        return self.verdict == GUARD_DENY


def parse_guard_verdict(text: Any) -> GuardDecision:
    """Parse a classifier reply into a :class:`GuardDecision`.

    Recognises the three verdict words case-insensitively anywhere in the
    reply and, when several appear, keeps the **strictest**. Anything empty,
    non-textual or unrecognised degrades to ``UNSURE`` — never ``ALLOW``.
    """
    if not isinstance(text, str) or not text.strip():
        return GuardDecision(GUARD_UNSURE, "empty classifier reply")
    upper = text.upper()
    found = [v for v in VALID_GUARD_VERDICTS if v.upper() in upper]
    if not found:
        return GuardDecision(GUARD_UNSURE, "unrecognised classifier reply")
    verdict = max(found, key=lambda v: _VERDICT_STRICTNESS[v])
    rationale = text.strip().splitlines()[0][:200]
    return GuardDecision(verdict, rationale)


def is_genuine_user_turn(message: Message) -> bool:
    """True when ``message`` is a real user turn (not tool output / env block).

    Tool results travel as ``role="user"`` messages with a
    :class:`ToolResultBlock`, and the host appends ``[env: …]`` blocks — both
    must stay out of the guardian's context.
    """
    if message.role != "user":
        return False
    content = message.content
    if isinstance(content, str):
        return not content.startswith(_ENV_CONTEXT_PREFIX)
    for block in content:
        if isinstance(block, ToolResultBlock):
            return False
    return True


def _message_text(message: Message) -> str:
    """Flatten a message to plain text (images and other blocks are dropped)."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = [block.text for block in content if isinstance(block, TextBlock)]
    return "\n".join(parts)


def build_guard_context(
    messages: list[Message],
    *,
    max_turns: int = 20,
    max_chars: int = 8000,
) -> list[Message]:
    """Build the guardian-safe view of the conversation.

    Keeps the **most recent** genuine user turns (see
    :func:`is_genuine_user_turn`) within a character budget, oldest dropped
    first. Assistant messages, tool results and env blocks are never included
    — that omission is the security property of this function, not an
    optimisation.
    """
    user_turns = [
        _message_text(message) for message in messages if is_genuine_user_turn(message)
    ]
    user_turns = [text for text in user_turns if text.strip()]

    # Keep the newest turns within both budgets.
    selected: list[str] = []
    total = 0
    for text in reversed(user_turns[-max_turns:]):
        if total + len(text) > max_chars and selected:
            break
        selected.append(text)
        total += len(text)
    selected.reverse()
    return [Message(role="user", content=text) for text in selected]


def render_pending_call(tool_call: ToolCallEvent) -> str:
    """Render the proposed action for the guardian, as compact JSON."""
    try:
        args = json.dumps(tool_call.args, sort_keys=True, default=str)
    except (TypeError, ValueError):  # non-serialisable args → opaque marker
        args = "<unserialisable>"
    return json.dumps(
        {"proposed_tool": tool_call.tool_name, "arguments": args},
        sort_keys=True,
    )


def build_guard_messages(
    user_context: list[Message],
    tool_call: ToolCallEvent,
    *,
    system_prompt: str = GUARD_SYSTEM_PROMPT,
) -> list[Message]:
    """Assemble the complete message list handed to the classifier.

    ``system guard prompt → genuine user turns → the pending action``. The
    *absence* of any assistant message is what stops the guarded agent from
    rationalising in front of its own guardian; it is enforced here, at the
    single point where the list is built.
    """
    return [
        Message(role="system", content=system_prompt),
        *user_context,
        Message(role="user", content=render_pending_call(tool_call)),
    ]


__all__ = [
    "GUARD_ALLOW",
    "GUARD_DENY",
    "GUARD_SYSTEM_PROMPT",
    "GUARD_UNSURE",
    "GuardClassifier",
    "GuardDecision",
    "VALID_GUARD_VERDICTS",
    "build_guard_context",
    "build_guard_messages",
    "is_genuine_user_turn",
    "parse_guard_verdict",
    "render_pending_call",
]
