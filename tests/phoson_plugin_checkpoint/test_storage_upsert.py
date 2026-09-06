"""Tests for F-52 — PostgresStorage.save() upsert (O(N) → O(delta)).

The core behaviour is tested against an in-memory fake of the asyncpg
connection, so the suite runs in any environment (no Docker needed):
the fake honours ``ON CONFLICT (session_id, id) DO NOTHING`` semantics
and records every SQL statement, which lets the tests assert that
``save()`` no longer issues a ``DELETE`` and that duplicate rows are
skipped instead of re-inserted.

One integration test runs against a real Postgres when reachable
(``docker compose -f docker-compose.test.yml up -d postgres-test``) and
skips otherwise — same convention as ``test_postgres_storage.py``.
"""

import os
from typing import Any
from datetime import UTC, datetime
from contextlib import asynccontextmanager

import pytest

from phoson_llm.schemas import Message
from phoson_agent.sessions.models import ConversationTree
from phoson_plugin_checkpoint.storage import (
    NODES_TABLE,
    SESSIONS_TABLE,
    PostgresStorage,
)

# asyncpg is an optional extra ("checkpoint"). The in-memory fake below only
# needs it to be importable; without it, the whole module is skipped (same
# convention as the provider adapter tests).
asyncpg = pytest.importorskip(
    "asyncpg", reason="asyncpg not installed (pip install asyncpg)"
)

DSN = os.environ.get(
    "PHOSON_TEST_POSTGRES_DSN",
    "postgresql://phoson:phoson@localhost:55432/phoson_test",
)


# ── In-memory fake of the asyncpg pieces PostgresStorage uses ─────────────


class FakeRow(dict[str, Any]):
    """asyncpg.Record stand-in: dict with ``row["col"]`` access."""


class FakeConn:
    """Minimal in-memory emulation of the SQL PostgresStorage issues.

    State:
        sessions: session_id → row dict (the sessions table).
        nodes: (session_id, id) → row dict (the nodes table).
        has_unique_constraint: whether UNIQUE (session_id, id) exists —
            what ``pg_constraint`` reports and what ``ON CONFLICT``
            enforces.
    """

    def __init__(self, has_unique_constraint: bool = True) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.has_unique_constraint = has_unique_constraint
        self.executed: list[str] = []  # every SQL statement, in order
        self.node_insert_sql: str = ""  # flattened nodes INSERT (executemany)
        self.inserted_rows: list[tuple[str, ...]] = []  # effective node rows
        self.skipped_rows: list[tuple[str, ...]] = []  # DO NOTHING skips

    @asynccontextmanager
    async def _cm(self) -> Any:
        yield self

    def transaction(self) -> Any:
        return self._cm()

    # ── statement handling ──────────────────────────────────────────────

    async def execute(self, sql: str, *params: Any) -> str:
        self.executed.append(sql)
        flat = " ".join(sql.split())
        if "INSERT INTO" in flat and SESSIONS_TABLE in flat:
            (
                session_id,
                total_cost,
                total_tokens,
                step_count,
                last_model,
            ) = params
            row = self.sessions.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "created_at": datetime.now(UTC),
                },
            )
            row.update(
                total_cost=total_cost,
                total_tokens=total_tokens,
                step_count=step_count,
                last_model=last_model,
                updated_at=datetime.now(UTC),
            )
            return "INSERT 0 1"
        if "ALTER TABLE" in flat and "ADD CONSTRAINT" in flat:
            self.has_unique_constraint = True
            return "ALTER TABLE"
        if "DELETE FROM" in flat and NODES_TABLE in flat:
            session_id = params[0]
            for key in [k for k in self.nodes if k[0] == session_id]:
                del self.nodes[key]
            return "DELETE 0"
        return "OK"  # CREATE TABLE / index DDL: no-op

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        flat = " ".join(sql.split())
        assert "INSERT INTO" in flat and NODES_TABLE in flat
        self.node_insert_sql = flat
        upsert = "ON CONFLICT (session_id, id) DO NOTHING" in flat
        for row in rows:
            session_id, node_id = row[0], row[1]
            key = (session_id, node_id)
            if upsert and key in self.nodes:
                self.skipped_rows.append(row)
                continue
            self.nodes[key] = {
                "session_id": session_id,
                "id": node_id,
                "parent_id": row[2],
                "message": row[3],
                "metadata": row[4],
                "created_at": row[5],
            }
            self.inserted_rows.append(row)

    async def fetch(self, sql: str, *params: Any) -> list[FakeRow]:
        flat = " ".join(sql.split())
        if "FROM pg_attribute" in flat:
            # (session_id, id) are the first two columns of the table.
            return [FakeRow(attnum=1), FakeRow(attnum=2)]
        if "FROM pg_constraint" in flat:
            if self.has_unique_constraint:
                return [FakeRow(conkey=(1, 2))]
            return []
        if flat.startswith("SELECT id, parent_id, message"):
            session_id = params[0]
            rows = [
                FakeRow(
                    id=r["id"],
                    parent_id=r["parent_id"],
                    message=r["message"],
                    metadata=r["metadata"],
                    created_at=r["created_at"],
                )
                for r in self.nodes.values()
                if r["session_id"] == session_id
            ]
            rows.sort(key=lambda r: r["created_at"])
            return rows
        return []

    async def fetchrow(self, sql: str, *params: Any) -> FakeRow | None:
        flat = " ".join(sql.split())
        if "FROM" in flat and SESSIONS_TABLE in flat:
            row = self.sessions.get(params[0])
            return FakeRow(**row) if row is not None else None
        return None


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._conn


def _make_storage(conn: FakeConn) -> PostgresStorage:
    """A PostgresStorage whose pool is pre-seeded with the fake conn."""
    storage = PostgresStorage(dsn="postgresql://fake")
    storage._pool = FakePool(conn)
    return storage


def _tree_with_nodes(session_id: str, n: int) -> ConversationTree:
    tree = ConversationTree.new(session_id=session_id)
    parent: str | None = None
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        parent = tree.append(
            parent_id=parent, message=Message(role=role, content=f"m{i}")
        ).id
    tree.update_session_meta(
        total_cost=0.01 * n, total_tokens=10 * n, step_count=n, last_model="test"
    )
    return tree


# ── F-52: save() upsert semantics (fake conn, no Postgres needed) ─────────


@pytest.mark.asyncio
async def test_save_does_not_delete_nodes() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    await storage.save(_tree_with_nodes("s1", 3))

    assert not any("DELETE FROM" in " ".join(s.split()) for s in conn.executed)
    assert len(conn.nodes) == 3


@pytest.mark.asyncio
async def test_save_node_insert_uses_on_conflict_do_nothing() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    await storage.save(_tree_with_nodes("s1", 2))

    # The nodes INSERT statement carries the upsert clause.
    assert "ON CONFLICT (session_id, id) DO NOTHING" in conn.node_insert_sql
    assert "DELETE" not in conn.node_insert_sql
    assert len(conn.nodes) == 2
    # Session row upserted (single session row, correct meta).
    assert set(conn.sessions) == {"s1"}
    assert conn.sessions["s1"]["total_tokens"] == 20


@pytest.mark.asyncio
async def test_save_twice_does_not_duplicate_nodes() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    tree = _tree_with_nodes("s1", 4)
    await storage.save(tree)
    await storage.save(tree)  # same tree again

    assert len(conn.nodes) == 4  # not 8
    assert len(conn.inserted_rows) == 4  # second save inserted nothing new
    assert len(conn.skipped_rows) == 4  # all 4 skipped via DO NOTHING


@pytest.mark.asyncio
async def test_save_incremental_appends_only_new_nodes() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    tree = _tree_with_nodes("s1", 100)
    await storage.save(tree)
    assert len(conn.inserted_rows) == 100

    # Simulate the next step: one new node appended to the live tree.
    last = max(tree.nodes, key=lambda nid: tree.nodes[nid].created_at)
    tree.append(parent_id=last, message=Message(role="user", content="m101"))
    await storage.save(tree)

    # All 101 rows are sent (Opción A), but only 1 is effectively written.
    assert len(conn.nodes) == 101
    assert len(conn.inserted_rows) == 101
    assert len(conn.skipped_rows) == 100


@pytest.mark.asyncio
async def test_duplicate_node_id_is_skipped_not_raised() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    tree = _tree_with_nodes("s1", 1)
    await storage.save(tree)
    # Re-save with an identical node id (conflict) — no error, no dup.
    await storage.save(tree)
    assert len(conn.nodes) == 1


@pytest.mark.asyncio
async def test_save_and_load_round_trip_through_fake() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    tree = _tree_with_nodes("s1", 3)
    await storage.save(tree)
    await storage.save(tree)

    loaded = await storage.load("s1")

    assert loaded.session_id == "s1"
    assert loaded.node_count() == 3
    assert loaded.total_tokens == 30
    assert loaded.last_model == "test"


@pytest.mark.asyncio
async def test_save_empty_tree_only_touches_session() -> None:
    conn = FakeConn()
    storage = _make_storage(conn)
    tree = ConversationTree.new(session_id="s-empty")
    await storage.save(tree)

    assert conn.nodes == {}
    assert conn.inserted_rows == []
    assert set(conn.sessions) == {"s-empty"}


# ── F-52: UNIQUE (session_id, id) constraint migration ────────────────────


@pytest.mark.asyncio
async def test_ensure_constraint_adds_it_when_missing() -> None:
    conn = FakeConn(has_unique_constraint=False)
    storage = _make_storage(conn)

    await storage._ensure_nodes_unique_constraint(conn)

    assert conn.has_unique_constraint is True
    alters = [s for s in conn.executed if "ALTER TABLE" in " ".join(s.split())]
    assert len(alters) == 1
    assert "UNIQUE (session_id, id)" in " ".join(alters[0].split())
    assert PostgresStorage._NODES_UQ_CONSTRAINT in alters[0]


@pytest.mark.asyncio
async def test_ensure_constraint_noop_when_present() -> None:
    conn = FakeConn(has_unique_constraint=True)
    storage = _make_storage(conn)

    await storage._ensure_nodes_unique_constraint(conn)

    assert not any("ALTER TABLE" in " ".join(s.split()) for s in conn.executed)


@pytest.mark.asyncio
async def test_ensure_constraint_failure_is_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingConn(FakeConn):
        async def execute(self, sql: str, *params: Any) -> str:
            if "ALTER TABLE" in " ".join(sql.split()):
                raise RuntimeError("duplicate key value violates constraint")
            return await super().execute(sql, *params)

    conn = _FailingConn(has_unique_constraint=False)
    storage = _make_storage(conn)

    with caplog.at_level("WARNING", logger="phoson_plugin_checkpoint"):
        await storage._ensure_nodes_unique_constraint(conn)  # must not raise

    assert any("Could not add UNIQUE" in r.message for r in caplog.records)


# ── F-52: integration against a real Postgres (skips when unreachable) ───


@pytest.fixture
async def pg_storage():
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


@pytest.mark.asyncio
async def test_pg_save_twice_no_duplicate_nodes(pg_storage: PostgresStorage) -> None:
    tree = _tree_with_nodes("f52-integration-1", 5)
    await pg_storage.save(tree)
    await pg_storage.save(tree)

    loaded = await pg_storage.load("f52-integration-1")
    assert loaded.node_count() == 5


@pytest.mark.asyncio
async def test_pg_upsert_survives_missing_constraint_migration(
    pg_storage: PostgresStorage,
) -> None:
    """Drop the PK (simulating a pre-F-52 table), then verify the
    migration in ``_ensure_pool`` re-adds a UNIQUE (session_id, id)
    constraint and the upsert still works."""
    session_id = "f52-integration-2"
    tree = _tree_with_nodes(session_id, 2)
    await pg_storage.save(tree)

    pool = await pg_storage._ensure_pool()
    async with pool.acquire() as conn:
        # Simulate a legacy table: no unique constraint on (session_id, id).
        await conn.execute(f"ALTER TABLE {NODES_TABLE} DROP CONSTRAINT PRIMARY KEY")

    # A fresh storage instance re-runs the migration on its own pool.
    second = PostgresStorage(dsn=DSN)
    try:
        await second._ensure_pool()
        await second.save(tree)
        await second.save(tree)
    finally:
        await second.close()

    loaded = await pg_storage.load(session_id)
    assert loaded.node_count() == 2
