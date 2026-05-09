"""Unit tests for phoson_cli.repl (PhosonRepl and SessionMetrics)."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_agent.models import (
    AgentDoneEvent,
    AgentRunResult,
    RunStep,
)
from phoson_agent.sessions.models import SessionMeta
from phoson_cli.config import PhosonConfig
from phoson_cli.repl import PhosonRepl, SessionMetrics

UTC = datetime.timezone.utc


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def repl(tmp_path):
    """A PhosonRepl instance with a mocked chat client."""
    with patch("phoson_cli.repl.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        return PhosonRepl(config)


# ── SessionMetrics ─────────────────────────────────────────────────────────────


def test_session_metrics_load_from_meta() -> None:
    """load_from_meta populates all fields from the dict."""
    metrics = SessionMetrics()
    meta = {
        "total_cost_usd": 1.23,
        "total_credits": 10.0,
        "total_input_tokens": 100,
        "total_output_tokens": 200,
        "total_cache_write_tokens": 5,
        "total_cache_read_tokens": 3,
        "step_count": 7,
        "last_model": "gpt-4",
    }
    metrics.load_from_meta(meta)

    assert metrics.total_cost_usd == 1.23
    assert metrics.total_credits == 10.0
    assert metrics.total_input_tokens == 100
    assert metrics.total_output_tokens == 200
    assert metrics.total_cache_write_tokens == 5
    assert metrics.total_cache_read_tokens == 3
    assert metrics.step_count == 7
    assert metrics.last_model == "gpt-4"


def test_session_metrics_to_meta_round_trip() -> None:
    """to_meta() → load_from_meta() round-trips cleanly."""
    original = SessionMetrics(
        total_cost_usd=0.5,
        total_credits=5.0,
        total_input_tokens=300,
        total_output_tokens=150,
        total_cache_write_tokens=10,
        total_cache_read_tokens=4,
        step_count=3,
        last_model="claude-3",
    )
    serialized = original.to_meta()

    restored = SessionMetrics()
    restored.load_from_meta(serialized)

    assert restored.total_cost_usd == original.total_cost_usd
    assert restored.total_credits == original.total_credits
    assert restored.total_input_tokens == original.total_input_tokens
    assert restored.total_output_tokens == original.total_output_tokens
    assert restored.total_cache_write_tokens == original.total_cache_write_tokens
    assert restored.total_cache_read_tokens == original.total_cache_read_tokens
    assert restored.step_count == original.step_count
    assert restored.last_model == original.last_model


# ── PhosonRepl._build_user_message ────────────────────────────────────────────


def test_build_user_message_plain_text(repl: PhosonRepl) -> None:
    """_build_user_message('hello') returns a user Message whose content contains the text."""
    msg = repl._build_user_message("hello")

    assert msg.role == "user"
    # Content is either a plain string or a list containing a TextBlock.
    content = msg.content
    if isinstance(content, str):
        assert "hello" in content
    else:
        from phoson_llm.schemas import TextBlock

        texts = [b.text for b in content if isinstance(b, TextBlock)]
        assert any("hello" in t for t in texts)


# ── PhosonRepl._append_user_turn ──────────────────────────────────────────────


def test_append_user_turn_adds_node(repl: PhosonRepl) -> None:
    """_append_user_turn grows the tree by one node and returns a path ending with the message."""
    from phoson_llm.schemas import Message

    before = len(repl.tree.nodes)
    msg = Message(role="user", content="test question")
    node_id, path = repl._append_user_turn(msg)

    assert len(repl.tree.nodes) == before + 1
    assert path[-1].content == "test question"
    assert node_id == repl.current_node_id


# ── PhosonRepl._finalize_run ──────────────────────────────────────────────────


def _make_run_step(cost: float = 0.001) -> RunStep:
    now = datetime.datetime.now(UTC)
    return RunStep(
        kind="llm",
        started_at=now,
        ended_at=now,
        duration_ms=100,
        cost_usd=cost,
        credits=1.0,
    )


def test_finalize_run_accumulates_metrics(repl: PhosonRepl) -> None:
    """_finalize_run updates session_metrics.step_count and total_cost_usd."""
    from phoson_llm.schemas import Message

    # Seed the tree with a user message so base_count is 1.
    user_msg = Message(role="user", content="hi")
    repl._append_user_turn(user_msg)
    base_count = 1

    steps = [_make_run_step(0.01), _make_run_step(0.02)]
    result = AgentRunResult(
        final_content="done",
        history=[user_msg],  # same as base_count, so no new messages appended
        input_messages=[user_msg],
        steps=steps,
        total_cost_usd=0.03,
    )
    done_event = AgentDoneEvent(result=result)

    # Patch summarizer to avoid real token estimation
    repl.summarizer.estimate_tokens = MagicMock(return_value=42)

    repl._finalize_run(done_event, base_count)

    assert repl.session_metrics.step_count == 2
    assert repl.session_metrics.total_cost_usd == pytest.approx(0.03)


# ── PhosonRepl._append_partial_history ────────────────────────────────────────


def test_append_partial_history_updates_node_id(repl: PhosonRepl) -> None:
    """_append_partial_history appends new messages and updates current_node_id."""
    from phoson_llm.schemas import Message

    user_msg = Message(role="user", content="partial question")
    repl._append_user_turn(user_msg)
    base_count = 1

    assistant_msg = Message(role="assistant", content="partial answer")
    partial_history = [user_msg, assistant_msg]  # base_count=1, so 1 new message

    repl.engine.get_partial_history = MagicMock(return_value=partial_history)

    old_node_id = repl.current_node_id
    repl._append_partial_history(base_count)

    # A new node was appended for the assistant message.
    assert repl.current_node_id != old_node_id or len(repl.tree.nodes) > 1


# ── PhosonRepl.load_session schema mapping ────────────────────────────────────


@pytest.mark.asyncio
async def test_load_session_schema_mapping(repl: PhosonRepl, tmp_path) -> None:
    """load_session maps SessionMeta fields to session_metrics correctly."""
    from phoson_agent.sessions.models import ConversationTree

    session_id = "abc12345"
    fake_tree = ConversationTree.new(session_id=session_id)
    now = datetime.datetime.now(UTC)

    meta = SessionMeta(
        id=session_id,
        created_at=now,
        updated_at=now,
        message_count=0,
        total_cost=1.5,
        total_tokens=500,
        step_count=3,
        last_model="gpt-4",
    )

    repl.storage.load = AsyncMock(return_value=fake_tree)
    repl.storage.list_meta = AsyncMock(return_value=[meta])

    # Patch print_history to avoid errors rendering empty history.
    repl.renderer.print_history = MagicMock()

    result = await repl.load_session(session_id)

    assert result is True
    assert repl.session_metrics.total_cost_usd == 1.5
    assert repl.session_metrics.total_output_tokens == 500
    assert repl.session_metrics.step_count == 3
    assert repl.session_metrics.last_model == "gpt-4"
