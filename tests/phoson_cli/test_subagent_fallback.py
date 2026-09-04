"""Unit tests for the sub-agent model fallback (issue #61).

Covers: successful fallback on availability errors, no fallback when
disabled (``main_model`` not injected) or on unrelated errors, and the
unavailability heuristic itself.
"""

import pytest

from phoson_llm.schemas import ModelConfig, LLMDoneEvent
from phoson_llm.chats.base import BaseLLMChat
from phoson_cli.tools.subagent import agent, agents, _is_model_unavailable_error


class _ProbeTool:
    """A real (non-delegation) tool so the sub-agent is not tool-less.

    ``_select_tools`` strips ``agent``/``agents`` (F-24), so a fixture that
    only offered those leaves the sub-agent with no tools.
    """

    name = "read_file"
    description = "read a file"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    async def handler(self, args, context=None):
        return "ok"


class UnavailableThenFallbackChat(BaseLLMChat):
    """Raises 404 for one model, succeeds for any other."""

    def __init__(self, bad_model: str) -> None:
        self.bad_model = bad_model
        self.calls: list[str] = []

    async def stream(self, messages, config: ModelConfig, tools=None):
        self.calls.append(config.model)
        if config.model == self.bad_model:
            error = RuntimeError("No endpoints found matching your request")
            error.status_code = 404  # type: ignore[attr-defined]
            raise error
        yield LLMDoneEvent(content=f"ok:{config.model}", has_tool_calls=False)


class AlwaysFailingChat(BaseLLMChat):
    """Fails with a non-availability error regardless of model."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stream(self, messages, config: ModelConfig, tools=None):
        self.calls.append(config.model)
        raise RuntimeError("invalid api key")
        yield LLMDoneEvent(content="", has_tool_calls=False)  # pragma: no cover


def _ctx(chat, *, with_main: bool = True) -> dict:
    ctx = {
        "chat": chat,
        "available_tools": {"read_file": _ProbeTool()},
        "default_model": "google/gemini-flash-lite-preview",
        "max_iterations": 2,
        "safe_mode": False,
        "subagent_timeout_seconds": 10.0,
    }
    if with_main:
        ctx["main_model"] = "anthropic/claude-3.5-haiku"
    return ctx


@pytest.mark.asyncio
async def test_fallback_to_main_model_on_404() -> None:
    chat = UnavailableThenFallbackChat("google/gemini-flash-lite-preview")
    result = await agent.handler({"task": "do it"}, _ctx(chat))
    assert "[fallback to anthropic/claude-3.5-haiku]" in result
    assert "ok:anthropic/claude-3.5-haiku" in result
    assert chat.calls == [
        "google/gemini-flash-lite-preview",
        "anthropic/claude-3.5-haiku",
    ]


@pytest.mark.asyncio
async def test_no_fallback_when_disabled() -> None:
    chat = UnavailableThenFallbackChat("google/gemini-flash-lite-preview")
    result = await agent.handler({"task": "do it"}, _ctx(chat, with_main=False))
    assert "Sub-agent error" in result
    assert chat.calls == ["google/gemini-flash-lite-preview"]


@pytest.mark.asyncio
async def test_no_fallback_on_unrelated_errors() -> None:
    chat = AlwaysFailingChat()
    result = await agent.handler({"task": "do it"}, _ctx(chat))
    assert "Sub-agent error" in result
    # Only the primary model was tried (the clone shares the calls list).
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_parallel_agents_fallback() -> None:
    chat = UnavailableThenFallbackChat("google/gemini-flash-lite-preview")
    result = await agents.handler({"tasks": ["a", "b"]}, _ctx(chat))
    assert "fallback_model=anthropic/claude-3.5-haiku" in result
    assert "ok:anthropic/claude-3.5-haiku" in result


def test_heuristic_classification() -> None:
    assert _is_model_unavailable_error(RuntimeError("HTTP 404: not found"))
    assert _is_model_unavailable_error(RuntimeError("model is deprecated"))
    exc = RuntimeError("quota exceeded")
    exc.status_code = 404  # type: ignore[attr-defined]
    assert _is_model_unavailable_error(exc)
    exc.status_code = 401  # type: ignore[attr-defined]
    assert not _is_model_unavailable_error(exc)
    exc.status_code = 429  # type: ignore[attr-defined]
    assert not _is_model_unavailable_error(exc)
    assert not _is_model_unavailable_error(RuntimeError("connection reset"))
