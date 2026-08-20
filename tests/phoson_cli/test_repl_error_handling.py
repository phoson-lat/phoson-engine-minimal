"""Regression tests: a failed agent run must not crash the REPL.

Before the fix, a run that failed (e.g. a 401 auth error from the
provider) ended the stream with an ``AgentErrorEvent`` and no
``AgentDoneEvent``; ``_consume_stream`` then raised
``RuntimeError("Agent stream ended without emitting AgentDoneEvent")``
and the REPL printed a traceback and exited. Now the terminal
``AgentErrorEvent`` is handled: the conversation is persisted, a hint
is shown for auth failures, and the REPL returns to the prompt.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_agent.models import (
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
)


def _make_repl(tmp_path) -> PhosonRepl:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        return PhosonRepl(config)


def _fake_stream(events):
    async def stream(path, config):
        for event in events:
            yield event

    return stream


def _stub_run_dependencies(repl: PhosonRepl) -> None:
    """Stub the async/network pieces around the stream loop."""
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)
    repl.storage.save = AsyncMock()
    repl.storage.save_meta = AsyncMock()


@pytest.mark.asyncio
async def test_agent_error_returns_to_prompt_instead_of_crashing(tmp_path) -> None:
    """The 401 scenario: stream ends with AgentErrorEvent, no DoneEvent."""
    repl = _make_repl(tmp_path)
    _stub_run_dependencies(repl)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentErrorEvent(message="Error code: 401 - missing auth", code="auth"),
    ]
    repl.engine.stream = _fake_stream(events)
    warn_calls: list[str] = []
    repl.renderer.print_warn = lambda msg: warn_calls.append(msg)

    await repl._run_agent("Hola!")  # must not raise

    # The conversation (user turn) is persisted.
    assert repl.storage.save.await_count >= 1
    assert repl.storage.save_meta.await_count >= 1
    assert len(repl.tree.nodes) == 1
    assert list(repl.tree.nodes.values())[0].message.role == "user"

    # Auth failures get an actionable hint.
    assert any("/setup" in msg for msg in warn_calls)


@pytest.mark.asyncio
async def test_agent_error_without_auth_code_no_hint(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    _stub_run_dependencies(repl)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentErrorEvent(message="boom", code="tool_error"),
    ]
    repl.engine.stream = _fake_stream(events)
    warn_calls: list[str] = []
    repl.renderer.print_warn = lambda msg: warn_calls.append(msg)

    await repl._run_agent("do it")

    assert not any("/setup" in msg for msg in warn_calls)


@pytest.mark.asyncio
async def test_agent_error_after_partial_steps_persists_partial_history(
    tmp_path,
) -> None:
    """Steps that succeeded before the failure stay in the tree."""
    repl = _make_repl(tmp_path)
    _stub_run_dependencies(repl)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentErrorEvent(message="upstream 500", code="llm"),
    ]
    repl.engine.stream = _fake_stream(events)
    partial = [
        Message(role="user", content="do it"),
        Message(role="assistant", content="Working on it..."),
    ]
    repl.engine.get_partial_history = lambda: list(partial)

    await repl._run_agent("do it")

    roles = [node.message.role for node in repl.tree.nodes.values()]
    assert roles == ["user", "assistant"]
    assert repl.current_node_id in repl.tree.nodes


@pytest.mark.asyncio
async def test_stream_without_terminal_event_still_raises(tmp_path) -> None:
    """A stream that ends with neither terminal event is a protocol bug."""
    repl = _make_repl(tmp_path)
    _stub_run_dependencies(repl)
    repl.engine.stream = _fake_stream([AgentStartEvent(model="m", message_count=1)])

    with pytest.raises(RuntimeError, match="terminal"):
        await repl._run_agent("do it")


@pytest.mark.asyncio
async def test_successful_run_still_finalizes(tmp_path) -> None:
    """The refactor must not change the happy path."""
    repl = _make_repl(tmp_path)
    _stub_run_dependencies(repl)
    result = AgentRunResult(
        final_content="done",
        history=[
            Message(role="user", content="do it"),
            Message(role="assistant", content="done"),
        ],
        input_messages=[Message(role="user", content="do it")],
    )
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentDoneEvent(result=result),
    ]
    repl.engine.stream = _fake_stream(events)

    await repl._run_agent("do it")

    assert repl.storage.save.await_count >= 1
    assert repl.storage.save_meta.await_count >= 1
    roles = [node.message.role for node in repl.tree.nodes.values()]
    assert roles == ["user", "assistant"]
