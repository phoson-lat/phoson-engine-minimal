"""Tests for #129 Slice 1: resumability hardening.

Covers:
- ``orphan_recovery`` (pure function over a node path): appends an error
  ``tool_result`` for an assistant node that ends on an unfinished
  ``tool_use``; idempotent; deterministic node id.
- ``status`` / ``last_run_id`` serialization round-trip (tree meta dict,
  JSONL storage, ``list_sessions``).
- Controller run-lifecycle status transitions (active → completed/aborted)
  and orphan detection + repair on ``load_session`` (the ``--resume`` path).
"""

import json
import asyncio
import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_agent import (
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
)
from phoson_llm.schemas import (
    Message,
    ToolUseBlock,
    ToolResultBlock,
)
from phoson_agent.sessions import (
    STATUS_ACTIVE,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    JsonlStorage,
    ConversationNode,
    ConversationTree,
    orphan_recovery,
)
from phoson_agent.sessions.serialization import (
    apply_tree_meta,
    tree_meta_to_dict,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _node(
    node_id: str,
    parent_id: str | None,
    message: Message,
    metadata: dict | None = None,
) -> ConversationNode:
    return ConversationNode(
        id=node_id,
        parent_id=parent_id,
        message=message,
        created_at=datetime.datetime.now(datetime.UTC),
        metadata=dict(metadata or {}),
    )


def _tool_use_message(*tool_call_ids: str) -> Message:
    return Message(
        role="assistant",
        content=[
            ToolUseBlock(tool_call_id=tid, tool_name="bash", args={})
            for tid in tool_call_ids
        ],
    )


def _tool_result_message(*tool_call_ids: str) -> Message:
    return Message(
        role="user",
        content=[
            ToolResultBlock(tool_call_id=tid, result="ok", error=False)
            for tid in tool_call_ids
        ],
    )


# ── orphan_recovery: pure function ───────────────────────────────────────────


def test_orphan_recovery_appends_tool_result_for_unfinished_tool_use():
    root = _node("n1", None, Message(role="user", content="run the tests"))
    orphan = _node("n2", "n1", _tool_use_message("tc_1"))
    nodes = orphan_recovery([root, orphan])

    assert len(nodes) == 3
    recovery = nodes[-1]
    assert recovery.parent_id == "n2"
    assert recovery.message.role == "user"
    assert recovery.metadata.get("recovery") is True
    assert len(recovery.message.content) == 1
    block = recovery.message.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_call_id == "tc_1"
    assert block.error is True
    assert "interrupted" in block.result.lower()


def test_orphan_recovery_no_change_when_tool_use_is_answered():
    root = _node("n1", None, Message(role="user", content="hi"))
    answered = _node(
        "n2",
        "n1",
        Message(
            role="assistant",
            content=[
                ToolUseBlock(tool_call_id="tc_1", tool_name="bash", args={}),
                ToolResultBlock(tool_call_id="tc_1", result="done", error=False),
            ],
        ),
    )
    nodes = orphan_recovery([root, answered])
    assert len(nodes) == 2  # untouched


def test_orphan_recovery_no_change_when_last_node_is_not_assistant():
    root = _node("n1", None, Message(role="user", content="hi"))
    last_user = _node("n2", "n1", Message(role="user", content="again"))
    assert len(orphan_recovery([root, last_user])) == 2


def test_orphan_recovery_no_change_for_plain_text_assistant():
    root = _node("n1", None, Message(role="user", content="hi"))
    text = _node("n2", "n1", Message(role="assistant", content="hello!"))
    assert len(orphan_recovery([root, text])) == 2


def test_orphan_recovery_empty_list():
    assert orphan_recovery([]) == []


def test_orphan_recovery_multiple_unfinished_tool_calls_all_answered_in_order():
    root = _node("n1", None, Message(role="user", content="do both"))
    orphan = _node("n2", "n1", _tool_use_message("tc_a", "tc_b"))
    nodes = orphan_recovery([root, orphan])

    recovery = nodes[-1]
    ids = [b.tool_call_id for b in recovery.message.content]
    assert ids == ["tc_a", "tc_b"]
    assert all(b.error for b in recovery.message.content)


def test_orphan_recovery_is_idempotent_on_same_list():
    root = _node("n1", None, Message(role="user", content="hi"))
    orphan = _node("n2", "n1", _tool_use_message("tc_1"))
    once = orphan_recovery([root, orphan])
    twice = orphan_recovery(once)
    assert len(twice) == len(once) == 3
    # Same node object, not a new one.
    assert twice[-1] is once[-1]


def test_orphan_recovery_idempotent_after_recovered_node_was_persisted():
    """Crash *after* the partial save that landed the recovery node: the
    loaded path already ends on the recovery node → nothing to repair."""
    root = _node("n1", None, Message(role="user", content="hi"))
    orphan = _node("n2", "n1", _tool_use_message("tc_1"))
    repaired = orphan_recovery([root, orphan])
    recovery = repaired[-1]

    # Reloaded path (root, orphan, recovery) → no further repair.
    reloaded = orphan_recovery(list(repaired))
    assert len(reloaded) == 3
    assert reloaded[-1] is recovery


def test_orphan_recovery_node_id_is_deterministic():
    root = _node("n1", None, Message(role="user", content="hi"))
    orphan = _node("n2", "n1", _tool_use_message("tc_1"))
    first = orphan_recovery([root, orphan])[-1]

    other_root = _node("n1", None, Message(role="user", content="hi"))
    other_orphan = _node("n2", "n1", _tool_use_message("tc_1"))
    second = orphan_recovery([other_root, other_orphan])[-1]

    assert first.id == second.id
    assert len(first.id) == 16


def test_orphan_recovery_different_parent_gets_different_id():
    root = _node("n1", None, Message(role="user", content="hi"))
    orphan = _node("n2", "n1", _tool_use_message("tc_1"))
    a = orphan_recovery([root, orphan])[-1]

    root_b = _node("n9", None, Message(role="user", content="hi"))
    orphan_b = _node("n8", "n9", _tool_use_message("tc_1"))
    b = orphan_recovery([root_b, orphan_b])[-1]

    assert a.id != b.id


# ── status / last_run_id serialization ───────────────────────────────────────


def test_tree_meta_round_trip_carries_status_and_run_id():
    tree = ConversationTree.new(session_id="s1")
    tree.status = STATUS_COMPLETED
    tree.last_run_id = "run-123"

    data = tree_meta_to_dict(tree)
    assert data["status"] == STATUS_COMPLETED
    assert data["last_run_id"] == "run-123"

    tree2 = ConversationTree.new(session_id="s2")
    apply_tree_meta(tree2, data)
    assert tree2.status == STATUS_COMPLETED
    assert tree2.last_run_id == "run-123"


def test_tree_meta_legacy_record_defaults_status_to_active():
    """A pre-#129 meta record (no status key) must read as 'active' so the
    session gets orphan-checked on resume, not assumed clean."""
    tree = ConversationTree.new(session_id="s1")
    apply_tree_meta(
        tree,
        {
            "type": "session_meta",
            "session_id": "s1",
            "total_cost": 0.1,
            "total_tokens": 10,
            "step_count": 1,
            "last_model": "m",
        },
    )
    assert tree.status == STATUS_ACTIVE
    assert tree.last_run_id is None


@pytest.mark.asyncio
async def test_storage_round_trip_persists_status_and_run_id(tmp_path):
    tree = ConversationTree.new(session_id="s1")
    tree.append(parent_id=None, message=Message(role="user", content="hi"))
    tree.status = STATUS_COMPLETED
    tree.last_run_id = "run-abc"

    storage = JsonlStorage(base_path=tmp_path)
    await storage.save(tree)

    loaded = await storage.load("s1")
    assert loaded.status == STATUS_COMPLETED
    assert loaded.last_run_id == "run-abc"

    metas = await storage.list_meta()
    assert metas[0].status == STATUS_COMPLETED
    assert metas[0].last_run_id == "run-abc"


@pytest.mark.asyncio
async def test_storage_save_meta_passes_status_through(tmp_path):
    tree = ConversationTree.new(session_id="s1")
    tree.append(parent_id=None, message=Message(role="user", content="hi"))
    storage = JsonlStorage(base_path=tmp_path)
    await storage.save(tree)

    await storage.save_meta(
        "s1",
        {
            "total_cost_usd": 0.2,
            "total_input_tokens": 5,
            "total_output_tokens": 7,
            "step_count": 2,
            "last_model": "m",
            "status": STATUS_ABORTED,
            "last_run_id": "run-xyz",
        },
    )

    loaded = await storage.load("s1")
    assert loaded.status == STATUS_ABORTED
    assert loaded.last_run_id == "run-xyz"
    assert loaded.total_cost == 0.2


@pytest.mark.asyncio
async def test_storage_save_meta_without_status_keeps_tree_value(tmp_path):
    """Legacy metrics dicts (no status key) must not clobber the status the
    controller set on the tree."""
    tree = ConversationTree.new(session_id="s1")
    tree.append(parent_id=None, message=Message(role="user", content="hi"))
    tree.status = STATUS_ACTIVE
    tree.last_run_id = "run-inflight"
    storage = JsonlStorage(base_path=tmp_path)
    await storage.save(tree)

    await storage.save_meta(
        "s1",
        {
            "total_cost_usd": 0.0,
            "total_input_tokens": 1,
            "total_output_tokens": 1,
            "step_count": 0,
            "last_model": "",
        },
    )

    loaded = await storage.load("s1")
    assert loaded.status == STATUS_ACTIVE
    assert loaded.last_run_id == "run-inflight"


@pytest.mark.asyncio
async def test_storage_legacy_file_reads_status_as_active(tmp_path):
    """A hand-written pre-#129 session file (no status in the meta record)
    must list as 'active' — i.e. 'may have died mid-run'."""
    file_path = tmp_path / "legacy.jsonl"
    meta = {
        "type": "session_meta",
        "session_id": "legacy",
        "total_cost": 0.0,
        "total_tokens": 0,
        "step_count": 0,
        "last_model": None,
        "title": None,
    }
    node = {
        "id": "n1",
        "parent_id": None,
        "message": {"role": "user", "content": "hi"},
        "created_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    file_path.write_text(
        json.dumps(meta) + "\n" + json.dumps(node) + "\n", encoding="utf-8"
    )

    metas = await JsonlStorage(base_path=tmp_path).list_sessions()
    assert len(metas) == 1
    assert metas[0].status == STATUS_ACTIVE
    assert metas[0].last_run_id is None


# ── Controller: run-lifecycle status + orphan repair on resume ──────────────


class _FakeSink:
    """Minimal recording AgentEventSink (no UI dependencies)."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.session_ids: list[str] = []
        self.history_calls: list = []

    def on_user_message(self, text, message) -> None:  # noqa: ARG002
        pass

    def on_attachments(self, sources) -> None:  # noqa: ARG002
        pass

    def on_event(self, event) -> None:  # noqa: ARG002
        pass

    def flush_line(self) -> None:
        pass

    def capture_partial_reasoning(self) -> None:
        pass

    def take_reasoning(self) -> str:
        return ""

    def set_session(self, session_id) -> None:
        self.session_ids.append(session_id)

    def print_history(self, path, tail=None) -> None:
        self.history_calls.append((path, tail))

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def on_subagent_progress(self, progress) -> None:  # noqa: ARG002
        pass


def _make_controller(tmp_path):
    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        return SessionController(config, _FakeSink())


def _done_event(answer="hello", history=None) -> AgentDoneEvent:
    if history is None:
        history = [
            Message(role="user", content="q"),
            Message(role="assistant", content=answer),
        ]
    return AgentDoneEvent(
        result=AgentRunResult(
            final_content=answer,
            history=history,
            input_messages=[history[0]],
            steps=[],
        )
    )


def _error_event(code="tool") -> AgentErrorEvent:
    return AgentErrorEvent(message="boom", code=code)


@pytest.mark.asyncio
async def test_run_turn_sets_status_completed_on_success(tmp_path):
    controller = _make_controller(tmp_path)

    async def stream(path, config):
        yield _done_event()

    controller.engine.stream = stream

    outcome = await controller.run_turn("q")
    assert outcome.status == "done"
    assert controller.tree.status == STATUS_COMPLETED
    assert controller.tree.last_run_id is not None

    # And it was persisted to disk.
    loaded = await controller.storage.load(controller.tree.session_id)
    assert loaded.status == STATUS_COMPLETED
    assert loaded.last_run_id == controller.tree.last_run_id


@pytest.mark.asyncio
async def test_run_turn_sets_status_aborted_on_error(tmp_path):
    controller = _make_controller(tmp_path)

    async def stream(path, config):
        yield _error_event()

    controller.engine.stream = stream

    outcome = await controller.run_turn("q")
    assert outcome.status == "error"
    assert controller.tree.status == STATUS_ABORTED

    loaded = await controller.storage.load(controller.tree.session_id)
    assert loaded.status == STATUS_ABORTED


@pytest.mark.asyncio
async def test_run_turn_sets_status_aborted_on_cancel(tmp_path):
    controller = _make_controller(tmp_path)

    async def stream(path, config):
        await asyncio.sleep(60)  # will be cancelled
        yield _done_event()  # pragma: no cover

    controller.engine.stream = stream

    run_task = asyncio.ensure_future(controller.run_turn("q"))
    while not controller.is_running:
        await asyncio.sleep(0.01)
    controller.cancel_current()
    outcome = await run_task

    assert outcome.status == "cancelled"
    assert controller.tree.status == STATUS_ABORTED


def _write_orphaned_session_file(
    tmp_path: Path, session_id: str = "orphan-001"
) -> None:
    """Write a session file that ends on an assistant tool_use with no
    tool_result and status='active' — i.e. a run that died mid-tool-call.

    Written by hand (not via JsonlStorage) so the test pins the on-disk
    shape that a *previous process* would have left behind.
    """
    now = "2026-01-01T00:00:00+00:00"
    meta = {
        "type": "session_meta",
        "session_id": session_id,
        "total_cost": 0.0,
        "total_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "step_count": 1,
        "last_model": "m",
        "title": None,
        "status": "active",
        "last_run_id": "run-dead",
    }
    root = {
        "id": "n1",
        "parent_id": None,
        "message": {"role": "user", "content": "hi"},
        "created_at": now,
        "metadata": {},
    }
    orphan = {
        "id": "n2",
        "parent_id": "n1",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "tool_call_id": "tc_dead",
                    "tool_name": "bash",
                    "args": {},
                }
            ],
        },
        "created_at": "2026-01-01T00:00:01+00:00",
        "metadata": {},
    }
    (tmp_path / f"{session_id}.jsonl").write_text(
        json.dumps(meta) + "\n" + json.dumps(root) + "\n" + json.dumps(orphan) + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_load_session_repairs_orphaned_tool_use(tmp_path):
    _write_orphaned_session_file(tmp_path)
    controller = _make_controller(tmp_path)

    outcome = await controller.load_session("orphan-001")
    assert outcome.ok

    # The recovery node was added and the cursor moved to it.
    path = controller.tree.get_node_path(controller.current_node_id)
    assert len(path) == 3
    recovery = path[-1]
    assert recovery.metadata.get("recovery") is True
    assert isinstance(recovery.message.content[0], ToolResultBlock)
    assert recovery.message.content[0].tool_call_id == "tc_dead"
    assert recovery.message.content[0].error is True

    # The user was told.
    warnings = [m for k, m in controller.sink.notifications if k == "warn"]
    assert any("orphaned" in m.lower() for m in warnings)

    # The repair was persisted.
    loaded = await controller.storage.load("orphan-001")
    loaded_path = loaded.get_node_path(loaded.get_leaves()[0])
    assert loaded_path[-1].metadata.get("recovery") is True


@pytest.mark.asyncio
async def test_load_session_orphan_repair_is_idempotent(tmp_path):
    _write_orphaned_session_file(tmp_path)
    controller = _make_controller(tmp_path)

    assert (await controller.load_session("orphan-001")).ok
    first_count = controller.tree.node_count()

    # Second load of the same (now repaired) session: no duplicate node.
    assert (await controller.load_session("orphan-001")).ok
    assert controller.tree.node_count() == first_count

    path = controller.tree.get_node_path(controller.current_node_id)
    recoveries = [n for n in path if n.metadata.get("recovery")]
    assert len(recoveries) == 1


@pytest.mark.asyncio
async def test_load_session_completed_session_not_touched(tmp_path):
    """A cleanly finished session must NOT get a recovery node even if it
    happens to end on a tool_use (e.g. the result was in a later branch)."""
    storage = JsonlStorage(base_path=tmp_path)
    tree = ConversationTree.new(session_id="clean-001")
    root = tree.append(parent_id=None, message=Message(role="user", content="hi"))
    tree.append(
        parent_id=root.id,
        message=_tool_use_message("tc_done"),
    )
    tree.status = STATUS_COMPLETED
    await storage.save(tree)

    controller = _make_controller(tmp_path)
    outcome = await controller.load_session("clean-001")
    assert outcome.ok
    assert controller.tree.node_count() == 2
    assert not any(
        "orphaned" in m.lower() for k, m in controller.sink.notifications if k == "warn"
    )


@pytest.mark.asyncio
async def test_load_session_legacy_file_orphan_gets_repaired(tmp_path):
    """A pre-#129 file (no status) that ends on an unfinished tool_use is
    treated as 'active' and repaired on resume."""
    file_path = tmp_path / "legacy-orphan.jsonl"
    meta = {
        "type": "session_meta",
        "session_id": "legacy-orphan",
        "total_cost": 0.0,
        "total_tokens": 0,
        "step_count": 1,
        "last_model": "m",
        "title": None,
    }
    root = {
        "id": "n1",
        "parent_id": None,
        "message": {"role": "user", "content": "hi"},
        "created_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    orphan = {
        "id": "n2",
        "parent_id": "n1",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "tool_call_id": "tc_legacy",
                    "tool_name": "bash",
                    "args": {},
                }
            ],
        },
        "created_at": "2026-01-01T00:00:01+00:00",
        "metadata": {},
    }
    file_path.write_text(
        json.dumps(meta) + "\n" + json.dumps(root) + "\n" + json.dumps(orphan) + "\n",
        encoding="utf-8",
    )

    controller = _make_controller(tmp_path)
    outcome = await controller.load_session("legacy-orphan")
    assert outcome.ok
    path = controller.tree.get_node_path(controller.current_node_id)
    assert len(path) == 3
    assert path[-1].metadata.get("recovery") is True


@pytest.mark.asyncio
async def test_load_session_orphan_repair_preserves_persisted_metrics(tmp_path):
    """Regression: the repair save must NOT clobber the persisted
    cost/token/step totals (the in-memory metrics are zero at load time)."""
    _write_orphaned_session_file(tmp_path)
    # Give the on-disk session real totals.
    file_path = tmp_path / "orphan-001.jsonl"
    lines = file_path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta.update(
        total_cost=0.75,
        total_tokens=500,
        total_input_tokens=300,
        total_output_tokens=200,
        step_count=4,
        last_model="qwen3-8b",
    )
    file_path.write_text(
        "\n".join([json.dumps(meta), *lines[1:]]) + "\n", encoding="utf-8"
    )

    controller = _make_controller(tmp_path)
    outcome = await controller.load_session("orphan-001")
    assert outcome.ok

    # The repair happened...
    path = controller.tree.get_node_path(controller.current_node_id)
    assert path[-1].metadata.get("recovery") is True
    # ...and the persisted totals survived the repair save.
    loaded = await controller.storage.load("orphan-001")
    assert loaded.total_cost == 0.75
    assert loaded.total_tokens == 500
    assert loaded.step_count == 4
    assert loaded.last_model == "qwen3-8b"
    assert loaded.status == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_load_session_clean_history_no_repair(tmp_path):
    """Normal resume regression: a completed session with a plain-text tail
    loads exactly as before (no recovery, no warn)."""
    storage = JsonlStorage(base_path=tmp_path)
    tree = ConversationTree.new(session_id="plain-001")
    root = tree.append(parent_id=None, message=Message(role="user", content="hi"))
    tree.append(
        parent_id=root.id,
        message=Message(role="assistant", content="hello!"),
    )
    tree.status = STATUS_COMPLETED
    await storage.save(tree)

    controller = _make_controller(tmp_path)
    outcome = await controller.load_session("plain-001")
    assert outcome.ok
    assert controller.tree.node_count() == 2
    assert controller.sink.notifications == []
