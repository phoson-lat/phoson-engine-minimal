"""Integration tests for PostgresStorage against a real Postgres instance.

Requires the ``postgres-test`` service from ``docker-compose.test.yml``:

    docker compose -f docker-compose.test.yml up -d postgres-test
    pytest tests/phoson_plugin_checkpoint -q

Tests are skipped automatically (not failed) when asyncpg isn't installed
or Postgres isn't reachable, so ``pytest -q`` stays green in environments
without Docker (e.g. plain CI without services).
"""

import os

import pytest

from phoson_llm.schemas import Message, TextBlock
from phoson_agent.sessions.models import ConversationTree

try:
    import asyncpg

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

if ASYNCPG_AVAILABLE:
    from phoson_plugin_checkpoint.storage import (
        NODES_TABLE,
        SESSIONS_TABLE,
        PostgresStorage,
    )

DSN = os.environ.get(
    "PHOSON_TEST_POSTGRES_DSN",
    "postgresql://phoson:phoson@localhost:55432/phoson_test",
)

pytestmark = pytest.mark.skipif(
    not ASYNCPG_AVAILABLE, reason="asyncpg not installed (pip install asyncpg)"
)


@pytest.fixture
async def storage():
    store = PostgresStorage(dsn=DSN)
    try:
        await store._ensure_pool()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"Postgres not reachable at {DSN}: {exc}. Start it with: "
            "docker compose -f docker-compose.test.yml up -d postgres-test"
        )

    yield store

    pool = await store._ensure_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {NODES_TABLE}")
        await conn.execute(f"DELETE FROM {SESSIONS_TABLE}")
    await store.close()


def _populated_tree(session_id: str) -> ConversationTree:
    tree = ConversationTree.new(session_id=session_id)
    root = tree.append(parent_id=None, message=Message(role="user", content="Hello"))
    tree.append(
        parent_id=root.id, message=Message(role="assistant", content="Hi there!")
    )
    tree.update_session_meta(
        total_cost=0.042, total_tokens=1200, step_count=5, last_model="gpt-4o-mini"
    )
    return tree


@pytest.mark.asyncio
async def test_save_and_load_full_conversation_tree(storage):
    tree = _populated_tree("pg-test-session-001")
    await storage.save(tree)

    loaded = await storage.load("pg-test-session-001")

    assert loaded.session_id == "pg-test-session-001"
    assert loaded.node_count() == 2
    assert loaded.total_cost == 0.042
    assert loaded.total_tokens == 1200
    assert loaded.step_count == 5
    assert loaded.last_model == "gpt-4o-mini"

    root_id = next(n.id for n in loaded.nodes.values() if n.parent_id is None)
    assert loaded.nodes[root_id].message.content == "Hello"
    child_id = next(n.id for n in loaded.nodes.values() if n.parent_id == root_id)
    assert loaded.nodes[child_id].message.content == "Hi there!"


@pytest.mark.asyncio
async def test_save_and_load_preserves_content_blocks(storage):
    tree = ConversationTree.new(session_id="pg-test-session-002")
    tree.append(
        parent_id=None,
        message=Message(role="user", content=[TextBlock(text="Hello")]),
    )
    await storage.save(tree)

    loaded = await storage.load("pg-test-session-002")

    node = next(iter(loaded.nodes.values()))
    assert isinstance(node.message.content, list)
    assert node.message.content[0].text == "Hello"


@pytest.mark.asyncio
async def test_save_is_idempotent_and_replaces_nodes(storage):
    tree = _populated_tree("pg-test-session-003")
    await storage.save(tree)

    tree2 = ConversationTree.new(session_id="pg-test-session-003")
    tree2.append(parent_id=None, message=Message(role="user", content="Different"))
    tree2.update_session_meta(total_cost=0.5, total_tokens=10, step_count=1)
    await storage.save(tree2)

    loaded = await storage.load("pg-test-session-003")
    assert loaded.node_count() == 1
    assert loaded.total_cost == 0.5


@pytest.mark.asyncio
async def test_load_raises_on_missing_session(storage):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        await storage.load("does-not-exist")


@pytest.mark.asyncio
async def test_list_sessions_sorted_by_updated_at(storage):
    await storage.save(_populated_tree("pg-test-session-a"))
    await storage.save(_populated_tree("pg-test-session-b"))

    sessions = await storage.list_sessions()

    ids = [s.id for s in sessions]
    assert "pg-test-session-a" in ids
    assert "pg-test-session-b" in ids
    assert sessions[0].updated_at >= sessions[-1].updated_at


@pytest.mark.asyncio
async def test_delete_removes_session_and_nodes(storage):
    tree = _populated_tree("pg-test-session-004")
    await storage.save(tree)

    await storage.delete("pg-test-session-004")

    with pytest.raises(FileNotFoundError):
        await storage.load("pg-test-session-004")


@pytest.mark.asyncio
async def test_delete_nonexistent_does_not_raise(storage):
    await storage.delete("never-existed")


@pytest.mark.asyncio
async def test_save_meta_updates_without_touching_nodes(storage):
    tree = _populated_tree("pg-test-session-005")
    await storage.save(tree)

    await storage.save_meta(
        "pg-test-session-005",
        {
            "total_cost_usd": 0.9,
            "total_input_tokens": 500,
            "total_output_tokens": 300,
            "step_count": 7,
            "last_model": "claude-3-haiku",
        },
    )

    loaded = await storage.load("pg-test-session-005")
    assert loaded.total_cost == 0.9
    assert loaded.total_tokens == 800
    assert loaded.step_count == 7
    assert loaded.last_model == "claude-3-haiku"
    assert loaded.node_count() == 2


@pytest.mark.asyncio
async def test_save_meta_raises_on_missing_session(storage):
    with pytest.raises(FileNotFoundError):
        await storage.save_meta("does-not-exist", {})


@pytest.mark.asyncio
async def test_list_meta_is_alias_for_list_sessions(storage):
    await storage.save(_populated_tree("pg-test-session-006"))

    meta_list = await storage.list_meta()
    sessions_list = await storage.list_sessions()

    assert [m.id for m in meta_list] == [s.id for s in sessions_list]
