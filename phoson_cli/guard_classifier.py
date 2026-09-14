"""Host-side LLM classifier for the permission guardian (issue #227 phase 3).

This is the concrete :data:`~phoson_agent.intent_guard.GuardClassifier` the CLI
injects into the :class:`~phoson_agent.permissions.PermissionMiddleware`. It is
deliberately tiny: the middleware builds the *hygiene-filtered* message list
(system guard prompt + genuine user turns + the pending action, no assistant
messages) and this only runs it through the chat client and returns the text.

The guardian runs **tool-free** and at ``temperature=0`` for a deterministic
verdict; a model error yields an empty reply, which the parser degrades to
``UNSURE`` — never ``ALLOW``. The timeout lives in the middleware (so the
``asyncio.wait_for`` wraps the whole call, including client hiccups).
"""

import logging
from typing import Any
from collections.abc import Callable

from phoson_llm.schemas import Message, ErrorEvent, TokenEvent, ModelConfig
from phoson_agent.intent_guard import GuardClassifier

from .config import PhosonConfig

_LOGGER = logging.getLogger("phoson_cli.guard")

#: The guardian only needs one word plus a short reason.
_GUARD_MAX_TOKENS = 96


async def run_llm_classifier(
    chat: Any,
    messages: list[Message],
    *,
    model: str,
    max_tokens: int = _GUARD_MAX_TOKENS,
) -> str:
    """Run the guardian classifier and return its raw reply text.

    Returns ``""`` on any error, which :func:`parse_guard_verdict` reads as
    ``UNSURE`` (fail closed). Never raises.
    """
    config = ModelConfig(
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        system=None,
    )
    text = ""
    try:
        async for event in chat.stream(messages, config):
            if isinstance(event, TokenEvent):
                text += event.content
            elif isinstance(event, ErrorEvent):
                _LOGGER.warning(
                    "guardian classifier error: %s", getattr(event, "message", "")
                )
                return ""
    except Exception:  # noqa: BLE001 — a broken guardian must not break the run
        _LOGGER.warning("guardian classifier call failed", exc_info=True)
        return ""
    return text


def build_permission_classifier(
    config: PhosonConfig,
    chat_getter: Callable[[], Any],
) -> GuardClassifier | None:
    """Build the guardian classifier from config, or ``None`` when disabled.

    ``chat_getter`` is a zero-arg callable returning the *current* chat client
    (resolved at call time), so the guardian survives engine rebuilds — the
    controller builds the permission middleware before the chat client exists.
    """
    if not getattr(config, "permission_classifier", False):
        return None

    async def classify(messages: list[Message]) -> str:
        chat = chat_getter()
        if chat is None:
            return ""
        model = config.permission_classifier_model or config.model
        return await run_llm_classifier(chat, messages, model=model)

    return classify


__all__ = ["build_permission_classifier", "run_llm_classifier"]
