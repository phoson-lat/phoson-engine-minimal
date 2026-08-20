import tempfile
from pathlib import Path

import pytest

from phoson_llm.schemas import Message, TextBlock
from phoson_agent.sessions.models import ConversationTree
from phoson_agent.sessions.storage_jsonl import JsonlStorage


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def sample_tree():
    tree = ConversationTree.new(session_id="test-session-001")
    tree.update_session_meta(
        total_cost=0.042,
        total_tokens=1200,
        step_count=5,
        last_model="gpt-4o-mini",
    )
    return tree


@pytest.fixture
def populated_tree():
    tree = ConversationTree.new(session_id="test-session-002")
    root = tree.append(
        parent_id=None,
        message=Message(role="user", content="Hello"),
    )
    tree.append(
        parent_id=root.id,
        message=Message(role="assistant", content="Hi there!"),
    )
    tree.update_session_meta(
        total_cost=0.001,
        total_tokens=50,
        step_count=1,
        last_model="gpt-4o-mini",
    )
    return tree


@pytest.mark.asyncio
async def test_save_and_load_empty_tree(temp_dir, sample_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(sample_tree)

    loaded = await storage.load("test-session-001")

    assert loaded.session_id == "test-session-001"
    assert loaded.total_cost == 0.042
    assert loaded.total_tokens == 1200
    assert loaded.step_count == 5
    assert loaded.last_model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_save_and_load_tree_with_nodes(temp_dir, populated_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(populated_tree)

    loaded = await storage.load("test-session-002")

    assert loaded.session_id == "test-session-002"
    assert loaded.node_count() == 2
    assert loaded.total_cost == 0.001


@pytest.mark.asyncio
async def test_save_and_load_preserves_message_content(temp_dir):
    tree = ConversationTree.new(session_id="test-content-001")
    tree.append(
        parent_id=None,
        message=Message(
            role="user",
            content=[
                TextBlock(text="Hello"),
            ],
        ),
    )

    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(tree)

    loaded = await storage.load("test-content-001")

    assert loaded.node_count() == 1
    node = list(loaded.nodes.values())[0]
    assert isinstance(node.message.content, list)
    assert node.message.content[0].text == "Hello"


@pytest.mark.asyncio
async def test_load_raises_on_missing_session(temp_dir):
    storage = JsonlStorage(base_path=temp_dir)

    with pytest.raises(FileNotFoundError, match="does not exist"):
        await storage.load("nonexistent-session")


@pytest.mark.asyncio
async def test_list_sessions_returns_sorted_by_updated(temp_dir, populated_tree):
    storage = JsonlStorage(base_path=temp_dir)

    await storage.save(populated_tree)

    tree2 = ConversationTree.new(session_id="test-session-003")
    tree2.append(
        parent_id=None,
        message=Message(role="user", content="Another message"),
    )
    await storage.save(tree2)

    sessions = await storage.list_sessions()

    assert len(sessions) == 2
    assert sessions[0].id == "test-session-003"
    assert sessions[1].id == "test-session-002"


@pytest.mark.asyncio
async def test_list_sessions_empty_dir(temp_dir):
    storage = JsonlStorage(base_path=temp_dir)
    sessions = await storage.list_sessions()
    assert sessions == []


@pytest.mark.asyncio
async def test_delete_removes_session_file(temp_dir, sample_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(sample_tree)

    assert (temp_dir / "test-session-001.jsonl").exists()

    await storage.delete("test-session-001")

    assert not (temp_dir / "test-session-001.jsonl").exists()


@pytest.mark.asyncio
async def test_delete_nonexistent_raises_no_error(temp_dir):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.delete("nonexistent-session")


@pytest.mark.asyncio
async def test_save_meta_updates_existing_tree(temp_dir, populated_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(populated_tree)

    await storage.save_meta(
        "test-session-002",
        {
            "total_cost_usd": 0.15,
            "total_input_tokens": 500,
            "total_output_tokens": 300,
            "step_count": 3,
            "last_model": "claude-3-haiku",
        },
    )

    loaded = await storage.load("test-session-002")
    assert loaded.total_cost == 0.15
    assert loaded.total_tokens == 800
    assert loaded.step_count == 3
    assert loaded.last_model == "claude-3-haiku"


@pytest.mark.asyncio
async def test_session_file_naming(temp_dir, sample_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(sample_tree)

    files = list(temp_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].stem == "test-session-001"


@pytest.mark.asyncio
async def test_multiple_sessions(temp_dir):
    storage = JsonlStorage(base_path=temp_dir)

    for i in range(5):
        tree = ConversationTree.new(session_id=f"session-{i:03d}")
        tree.append(
            parent_id=None,
            message=Message(role="user", content=f"Message {i}"),
        )
        await storage.save(tree)

    sessions = await storage.list_sessions()
    assert len(sessions) == 5
    assert sessions[0].id == "session-004"
    assert sessions[4].id == "session-000"


@pytest.mark.asyncio
async def test_list_meta_is_alias_for_list_sessions(temp_dir, populated_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(populated_tree)

    meta_list = await storage.list_meta()
    sessions_list = await storage.list_sessions()

    assert len(meta_list) == len(sessions_list)
    assert meta_list[0].id == sessions_list[0].id


# ── Atomicity / robustness ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_is_atomic_no_tmp_files_left_behind(temp_dir, populated_tree):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(populated_tree)

    leftover = list(temp_dir.glob("*.tmp.*"))
    assert leftover == []


@pytest.mark.asyncio
async def test_save_does_not_corrupt_existing_file_on_serialization_error(
    temp_dir, populated_tree, monkeypatch
):
    """If serialization blows up mid-write, the previous file must survive intact."""
    storage = JsonlStorage(base_path=temp_dir)
    await storage.save(populated_tree)

    # Capture the bytes of the good file.
    good_bytes = (temp_dir / f"{populated_tree.session_id}.jsonl").read_bytes()

    # Force ``node_to_dict`` to explode on the next save.
    import phoson_agent.sessions.storage_jsonl as mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated serializer crash")

    monkeypatch.setattr(mod, "node_to_dict", _boom)

    with pytest.raises(RuntimeError):
        await storage.save(populated_tree)

    # The previous good file must still be there, unchanged.
    survivor = (temp_dir / f"{populated_tree.session_id}.jsonl").read_bytes()
    assert survivor == good_bytes
    # And no orphaned tmp file.
    assert list(temp_dir.glob("*.tmp.*")) == []


@pytest.mark.asyncio
async def test_list_sessions_skips_malformed_lines(temp_dir):
    storage = JsonlStorage(base_path=temp_dir)
    bad_file = temp_dir / "broken.jsonl"
    bad_file.write_text("this is not json\n{also broken\n", encoding="utf-8")

    # No exception, file just gets skipped.
    sessions = await storage.list_sessions()

    assert all(s.id != "broken" for s in sessions)


@pytest.mark.asyncio
async def test_delete_nonexistent_session_does_not_raise(temp_dir):
    storage = JsonlStorage(base_path=temp_dir)
    await storage.delete("does-not-exist")  # must not raise


@pytest.mark.asyncio
async def test_list_sessions_restores_persisted_meta_totals(temp_dir):
    """Regression: list_sessions must read cost/tokens/steps/model from the
    persisted session_meta record, not return zeros."""
    storage = JsonlStorage(base_path=temp_dir)
    tree = ConversationTree.new(session_id="meta-roundtrip")
    tree.append(parent_id=None, message=Message(role="user", content="Hi"))
    await storage.save(tree)
    await storage.save_meta(
        "meta-roundtrip",
        {
            "total_cost_usd": 1.25,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "step_count": 7,
            "last_model": "claude-3-haiku",
        },
    )

    metas = await storage.list_meta()

    assert len(metas) == 1
    meta = metas[0]
    assert meta.id == "meta-roundtrip"
    assert meta.total_cost == 1.25
    assert meta.total_tokens == 150
    assert meta.step_count == 7
    assert meta.last_model == "claude-3-haiku"
    assert meta.message_count == 1
