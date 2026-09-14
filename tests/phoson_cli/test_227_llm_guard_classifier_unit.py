"""Tests for the host-side LLM guardian classifier (issue #227 phase 3).

Exercises the concrete :class:`GuardClassifier` the CLI injects: it runs the
hygiene-filtered messages through the chat client, returns the reply text, and
**fails closed to an empty reply** on any error (which the parser reads as
UNSURE, never ALLOW).
"""

from dataclasses import dataclass
from collections.abc import AsyncIterator

import pytest

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    TokenEvent,
    ModelConfig,
)
from phoson_cli.guard_classifier import (
    run_llm_classifier,
    build_permission_classifier,
)


@dataclass
class _FakeConfig:
    """Stand-in for PhosonConfig (only the guardian fields are read)."""

    permission_classifier: bool = True
    permission_classifier_model: str = ""
    model: str = "main-model"


class _FakeChat:
    """Minimal chat client: replays scripted events for every stream call."""

    def __init__(self, events: list) -> None:
        self._events = events
        self.seen: list[tuple[list[Message], ModelConfig]] = []

    async def stream(
        self, messages: list[Message], config: ModelConfig
    ) -> AsyncIterator:
        self.seen.append((list(messages), config))
        for event in self._events:
            yield event


async def test_run_llm_classifier_concatenates_tokens() -> None:
    chat = _FakeChat([TokenEvent(content="ALLOW"), TokenEvent(content="\nfine")])
    reply = await run_llm_classifier(
        chat, [Message(role="user", content="x")], model="guard"
    )
    assert reply == "ALLOW\nfine"
    # Deterministic, tool-free, low-token config.
    (_messages, config) = chat.seen[0]
    assert config.model == "guard"
    assert config.temperature == 0.0
    assert config.system is None


async def test_run_llm_classifier_error_event_returns_empty() -> None:
    chat = _FakeChat([TokenEvent(content="ALL"), ErrorEvent(message="boom")])
    reply = await run_llm_classifier(
        chat, [Message(role="user", content="x")], model="guard"
    )
    assert reply == ""  # → UNSURE, never ALLOW


async def test_run_llm_classifier_exception_returns_empty() -> None:
    class _Boom:
        async def stream(self, messages, config):
            raise RuntimeError("network down")
            yield  # pragma: no cover - unreachable, makes this an async gen

    reply = await run_llm_classifier(
        _Boom(), [Message(role="user", content="x")], model="guard"
    )
    assert reply == ""


async def test_build_permission_classifier_disabled_returns_none() -> None:
    assert (
        build_permission_classifier(
            _FakeConfig(permission_classifier=False), lambda: None
        )
        is None
    )


async def test_build_permission_classifier_uses_config_model_then_fallback() -> None:
    chat = _FakeChat([TokenEvent(content="DENY")])

    # Explicit guard model wins.
    classify = build_permission_classifier(
        _FakeConfig(permission_classifier_model="guard-model"), lambda: chat
    )
    assert classify is not None
    assert await classify([Message(role="user", content="x")]) == "DENY"
    assert chat.seen[0][1].model == "guard-model"

    # Empty guard model falls back to the main model.
    chat2 = _FakeChat([TokenEvent(content="ALLOW")])
    classify2 = build_permission_classifier(_FakeConfig(), lambda: chat2)
    assert classify2 is not None
    await classify2([Message(role="user", content="x")])
    assert chat2.seen[0][1].model == "main-model"


async def test_build_permission_classifier_without_chat_fails_closed() -> None:
    classify = build_permission_classifier(_FakeConfig(), lambda: None)
    assert classify is not None
    assert await classify([Message(role="user", content="x")]) == ""


@pytest.mark.parametrize("enabled", [True, False])
def test_build_permission_classifier_respects_flag(enabled) -> None:
    result = build_permission_classifier(
        _FakeConfig(permission_classifier=enabled), lambda: None
    )
    assert (result is not None) is enabled
