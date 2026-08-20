"""Unit tests for phoson_cli.repl (PhosonRepl and SessionMetrics)."""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.repl import PhosonRepl, SessionMetrics
from phoson_cli.config import PhosonConfig
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
)
from phoson_agent.sessions.models import SessionMeta, ConversationTree

UTC = datetime.UTC


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def repl(tmp_path):
    """A PhosonRepl instance with a mocked chat client."""
    with patch("phoson_cli.controller.build_chat") as mock_build:
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
    """_build_user_message wraps plain text in a user Message."""
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
    """_append_user_turn grows the tree by one node; path ends with the new message."""
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


# ── PhosonRepl._rebuild_engine: old runtime cleanup ─────────────────────────


def _fake_engine_with_plugins(*plugins) -> MagicMock:
    engine = MagicMock()
    engine._loaded_plugins = list(plugins)
    return engine


class _AsyncClosePlugin:
    """Plugin with async aclose() (like the MCP plugin)."""

    name = "mcp"

    def __init__(self) -> None:
        self.aclose_calls = 0
        self.fail = False

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self.fail:
            raise RuntimeError("boom")


class _SyncOnlyPlugin:
    """Plugin with only sync cleanup()."""

    name = "memory"

    def __init__(self) -> None:
        self.cleaned = 0

    def cleanup(self) -> None:
        self.cleaned += 1


@pytest.mark.asyncio
async def test_rebuild_engine_closes_old_plugins_and_chat(repl: PhosonRepl) -> None:
    """Switching model/provider must close the previous engine's plugins
    (e.g. MCP pooled sessions) and the old chat client."""
    async_plugin = _AsyncClosePlugin()
    sync_plugin = _SyncOnlyPlugin()

    old_chat = MagicMock()
    old_chat.aclose = AsyncMock()

    repl.chat = old_chat
    repl.engine = _fake_engine_with_plugins(async_plugin, sync_plugin)

    new_engine = MagicMock()
    new_engine._loaded_plugins = []
    with patch("phoson_cli.controller.AgentEngine", return_value=new_engine):
        repl._rebuild_engine()

    # The close is scheduled as a task — give the loop a tick to run it.
    await asyncio.sleep(0.05)

    assert async_plugin.aclose_calls == 1
    assert sync_plugin.cleaned == 1
    old_chat.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebuild_engine_survives_plugin_close_failure(repl: PhosonRepl) -> None:
    """A failing plugin close must not break the rebuild — the new engine
    is in place and the failure is logged, not raised."""
    bad_plugin = _AsyncClosePlugin()
    bad_plugin.fail = True

    old_chat = MagicMock()
    old_chat.aclose = AsyncMock()

    repl.chat = old_chat
    repl.engine = _fake_engine_with_plugins(bad_plugin)

    new_engine = MagicMock()
    new_engine._loaded_plugins = []
    with patch("phoson_cli.controller.AgentEngine", return_value=new_engine):
        repl._rebuild_engine()  # must not raise

    await asyncio.sleep(0.05)

    assert repl.engine is new_engine
    assert bad_plugin.aclose_calls == 1


# ── PhosonRepl.find_latest_node_id ───────────────────────────────────────────


def test_find_latest_node_id_empty_tree(repl: PhosonRepl) -> None:
    fresh = ConversationTree.new(session_id="x")
    repl.tree = fresh
    repl.current_node_id = None
    assert repl.find_latest_node_id() is None


def test_find_latest_node_id_returns_newest_leaf(repl: PhosonRepl) -> None:
    """The continuation point is the newest *leaf*; internal nodes and
    stale leaves on other branches are ignored."""
    from phoson_llm.schemas import Message

    tree = ConversationTree.new(session_id="s1")
    root = tree.append(None, Message(role="user", content="root"))
    a = tree.append(root.id, Message(role="assistant", content="branch A"))
    b = tree.append(root.id, Message(role="assistant", content="branch B"))

    # Make branch A the stale one (older than B) and B's continuation the
    # newest leaf.
    older = root.created_at - datetime.timedelta(seconds=10)
    a.created_at = older
    continuation = tree.append(b.id, Message(role="user", content="continue on B"))

    repl.tree = tree
    repl.current_node_id = None

    assert repl.find_latest_node_id() == continuation.id


# ── PhosonRepl._build_system_prompt ──────────────────────────────────────────


def test_system_prompt_lists_real_tool_names(repl: PhosonRepl) -> None:
    prompt = repl._build_system_prompt()

    assert "agent, agents" in prompt
    assert "subagents" not in prompt
    assert "MCP tools" not in prompt


def test_system_prompt_mentions_mcp_tools_when_loaded(repl: PhosonRepl) -> None:
    fake_mcp_tool = MagicMock()
    fake_mcp_tool.name = "mcp_github_get_user"
    plain_tool = MagicMock()
    plain_tool.name = "bash"
    repl.engine.tools = [plain_tool, fake_mcp_tool]

    prompt = repl._build_system_prompt()

    assert "MCP tools (names prefixed 'mcp_') are also available" in prompt


# ── PhosonRepl.undo_last_turn ─────────────────────────────────────────────────


def _build_two_turn_tree(repl: PhosonRepl) -> tuple:
    """Build: u1 → a1 → u2 → a2, with the cursor at a2. Returns (u1, a1, u2, a2)."""
    from phoson_llm.schemas import Message

    repl._append_user_turn(Message(role="user", content="q1"))
    a1 = repl.tree.append(repl.current_node_id, Message(role="assistant", content="a1"))
    repl.current_node_id = a1.id
    u2, _ = repl._append_user_turn(Message(role="user", content="q2"))
    a2 = repl.tree.append(repl.current_node_id, Message(role="assistant", content="a2"))
    repl.current_node_id = a2.id
    u1 = next(n for n in repl.tree.nodes.values() if n.message.content == "q1")
    return u1, a1, u2, a2


def test_undo_last_turn_moves_cursor_before_last_user(repl: PhosonRepl) -> None:
    from phoson_llm.schemas import Message

    _u1, a1, _u2, a2 = _build_two_turn_tree(repl)

    ok, info = repl.undo_last_turn()

    assert ok is True
    assert repl.current_node_id == a1.id  # cursor → node before the last user turn
    assert info == a1.id
    # The undone branch still exists in the tree.
    assert a2.id in repl.tree.nodes

    # The next user message branches from a1 (sibling of the undone u2).
    u3_id, path = repl._append_user_turn(Message(role="user", content="q2-retry"))
    assert repl.tree.nodes[u3_id].parent_id == a1.id
    assert path[-1].content == "q2-retry"


def test_undo_single_turn_reports_nothing_to_undo(repl: PhosonRepl) -> None:
    from phoson_llm.schemas import Message

    repl._append_user_turn(Message(role="user", content="only turn"))

    ok, info = repl.undo_last_turn()

    assert ok is False
    assert "session starts with this turn" in info
    # Cursor untouched.
    assert repl.current_node_id is not None


def test_undo_without_active_node(repl: PhosonRepl) -> None:
    repl.current_node_id = None

    ok, info = repl.undo_last_turn()

    assert ok is False
    assert "No active node" in info


def test_undo_keeps_metrics_untouched(repl: PhosonRepl) -> None:
    """Session cost/token metrics are cumulative — undo must not rewind them."""
    _u1, _a1, _u2, _a2 = _build_two_turn_tree(repl)

    repl.session_metrics.total_cost_usd = 1.25
    repl.session_metrics.step_count = 7

    ok, _ = repl.undo_last_turn()

    assert ok is True
    assert repl.session_metrics.total_cost_usd == 1.25
    assert repl.session_metrics.step_count == 7
