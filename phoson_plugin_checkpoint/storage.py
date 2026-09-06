"""Postgres-backed SessionStorage implementation.

Persists ConversationTree sessions in their own schema (two tables, prefixed
``phoson_checkpoint_``) so this plugin never depends on tables owned by the
application embedding the engine (e.g. Phoson-Core).
"""

import json
import asyncio
import logging
from typing import Any
from dataclasses import field, dataclass

try:
    import asyncpg

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

from phoson_agent.exceptions import PhosonSessionNotFoundError
from phoson_agent.sessions.models import (
    SessionMeta,
    SessionStorage,
    ConversationNode,
    ConversationTree,
)
from phoson_agent.sessions.serialization import message_to_dict, message_from_dict

SESSIONS_TABLE = "phoson_checkpoint_sessions"
NODES_TABLE = "phoson_checkpoint_nodes"

_LOG = logging.getLogger("phoson_plugin_checkpoint")

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {SESSIONS_TABLE} (
    session_id TEXT PRIMARY KEY,
    total_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    step_count INTEGER NOT NULL DEFAULT 0,
    last_model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {NODES_TABLE} (
    session_id TEXT NOT NULL REFERENCES {SESSIONS_TABLE}(session_id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    parent_id TEXT,
    message JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS phoson_checkpoint_nodes_session_id_idx
    ON {NODES_TABLE} (session_id);
"""


def _json_field(value: Any) -> Any:
    """asyncpg returns jsonb as str unless a codec is registered; accept both."""
    return json.loads(value) if isinstance(value, str) else value


@dataclass
class PostgresStorage(SessionStorage):
    """Postgres-backed session storage for conversation trees.

    Every I/O method is genuinely async (asyncpg native protocol, no
    ``asyncio.to_thread`` wrapping a blocking driver).

    Args:
        dsn: PostgreSQL connection string, e.g.
            ``postgresql://user:pass@host:5432/dbname``.
        pool_min_size: Minimum pool connections.
        pool_max_size: Maximum pool connections.

    Example:
        storage = PostgresStorage(dsn="postgresql://phoson:phoson@localhost/phoson")
        await storage.save(tree)
        sessions = await storage.list_sessions()
        await storage.close()
    """

    dsn: str
    pool_min_size: int = 1
    pool_max_size: int = 10

    #: Name of the UNIQUE (session_id, id) constraint the upsert in
    #: :meth:`save` relies on (F-52). Fresh installs get it from the
    #: ``PRIMARY KEY`` in ``_SCHEMA_SQL``; pre-F-52 tables get it via the
    #: migration in :meth:`_ensure_pool`.
    _NODES_UQ_CONSTRAINT = "uq_nodes_session_id"

    _pool: Any = field(default=None, init=False, repr=False)
    _pool_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    async def _ensure_pool(self) -> Any:
        if not ASYNCPG_AVAILABLE:
            raise ImportError(
                "asyncpg not installed. Install with: pip install asyncpg "
                "or pip install 'phoson-engine-minimal[checkpoint]'"
            )
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=self.pool_min_size,
                    max_size=self.pool_max_size,
                )
                async with pool.acquire() as conn:
                    await conn.execute(_SCHEMA_SQL)
                    await self._ensure_nodes_unique_constraint(conn)
                self._pool = pool
        return self._pool

    async def _ensure_nodes_unique_constraint(self, conn: Any) -> None:
        """Ensure the UNIQUE (session_id, id) constraint exists (F-52).

        ``ON CONFLICT (session_id, id) DO NOTHING`` in :meth:`save`
        requires a unique constraint on that column pair. Fresh installs
        already have one (the ``PRIMARY KEY`` in ``_SCHEMA_SQL``); this
        covers pre-F-52 tables created without it.

        Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``, so the
        constraint is checked first (``pg_constraint``) and the
        ``ALTER TABLE`` only runs when it is missing. A failure (e.g.
        duplicate rows the constraint cannot coexist with) is logged,
        not raised: the storage must stay usable and the caller can
        clean the duplicates and retry.
        """
        # The column attnums of (session_id, id) in the table's column
        # order — what a UNIQUE constraint on that pair would store in
        # pg_constraint.conkey.
        attnums = await conn.fetch(
            """
            SELECT attnum FROM pg_attribute
            WHERE attrelid = $1::regclass AND attname IN ('session_id', 'id')
            ORDER BY attnum
            """,
            NODES_TABLE,
        )
        expected = tuple(row["attnum"] for row in attnums)
        existing = await conn.fetch(
            "SELECT conkey FROM pg_constraint WHERE conrelid = $1::regclass",
            NODES_TABLE,
        )
        if expected and tuple(expected) in [tuple(row["conkey"]) for row in existing]:
            return
        try:
            await conn.execute(
                f"""
                ALTER TABLE {NODES_TABLE}
                ADD CONSTRAINT {self._NODES_UQ_CONSTRAINT}
                    UNIQUE (session_id, id)
                """
            )
        except Exception as exc:  # noqa: BLE001 — best-effort migration
            _LOG.warning(
                "Could not add UNIQUE (session_id, id) constraint to %s: %s. "
                "save() upserts may misbehave until it is added manually.",
                NODES_TABLE,
                exc,
            )

    async def close(self) -> None:
        """Close the connection pool. Safe to call even if never opened."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ── SessionStorage API ───────────────────────────────────────────────

    async def save(self, tree: ConversationTree) -> None:
        """Persist a conversation tree incrementally (F-52).

        Append-only upsert: nodes whose ``(session_id, id)`` already
        exists are skipped via ``ON CONFLICT (session_id, id) DO
        NOTHING`` instead of the old ``DELETE`` + full re-``INSERT``.
        A session's nodes only ever grow (compaction appends a new
        branch; a fresh session gets a fresh id), so skipping existing
        rows is semantically identical to the old replace — and it turns
        each save from O(N) writes into O(delta) effective writes.
        """
        pool = await self._ensure_pool()
        nodes = sorted(tree.nodes.values(), key=lambda n: n.created_at)

        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"""
                INSERT INTO {SESSIONS_TABLE}
                    (session_id, total_cost, total_tokens, step_count,
                     last_model, updated_at)
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (session_id) DO UPDATE SET
                    total_cost = EXCLUDED.total_cost,
                    total_tokens = EXCLUDED.total_tokens,
                    step_count = EXCLUDED.step_count,
                    last_model = EXCLUDED.last_model,
                    updated_at = now()
                """,
                tree.session_id,
                tree.total_cost,
                tree.total_tokens,
                tree.step_count,
                tree.last_model,
            )
            if nodes:
                await conn.executemany(
                    f"""
                    INSERT INTO {NODES_TABLE}
                        (session_id, id, parent_id, message, metadata, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (session_id, id) DO NOTHING
                    """,
                    [
                        (
                            tree.session_id,
                            node.id,
                            node.parent_id,
                            json.dumps(message_to_dict(node.message)),
                            json.dumps(node.metadata),
                            node.created_at,
                        )
                        for node in nodes
                    ],
                )

    async def load(self, session_id: str) -> ConversationTree:
        """Load a conversation tree by session ID."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            session_row = await conn.fetchrow(
                f"SELECT * FROM {SESSIONS_TABLE} WHERE session_id = $1", session_id
            )
            if session_row is None:
                raise PhosonSessionNotFoundError(
                    f"Session {session_id} does not exist.",
                    session_id=session_id,
                )
            node_rows = await conn.fetch(
                f"""
                SELECT id, parent_id, message, metadata, created_at
                FROM {NODES_TABLE}
                WHERE session_id = $1
                ORDER BY created_at
                """,
                session_id,
            )

        tree = ConversationTree.new(session_id=session_id)
        tree.update_session_meta(
            total_cost=session_row["total_cost"],
            total_tokens=session_row["total_tokens"],
            step_count=session_row["step_count"],
            last_model=session_row["last_model"],
        )
        for row in node_rows:
            tree.add_node(
                ConversationNode(
                    id=row["id"],
                    parent_id=row["parent_id"],
                    message=message_from_dict(_json_field(row["message"])),
                    created_at=row["created_at"],
                    metadata=_json_field(row["metadata"]) or {},
                )
            )
        return tree

    async def list_sessions(self) -> list[SessionMeta]:
        """List all available sessions, most recently updated first."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    s.session_id,
                    s.total_cost,
                    s.total_tokens,
                    s.step_count,
                    s.last_model,
                    s.updated_at,
                    COALESCE(n.first_created_at, s.created_at) AS created_at,
                    COALESCE(n.message_count, 0) AS message_count
                FROM {SESSIONS_TABLE} s
                LEFT JOIN (
                    SELECT session_id,
                           MIN(created_at) AS first_created_at,
                           COUNT(*) AS message_count
                    FROM {NODES_TABLE}
                    GROUP BY session_id
                ) n ON n.session_id = s.session_id
                ORDER BY s.updated_at DESC
                """
            )
        return [
            SessionMeta(
                id=row["session_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=row["message_count"],
                total_cost=row["total_cost"],
                total_tokens=row["total_tokens"],
                step_count=row["step_count"],
                last_model=row["last_model"],
            )
            for row in rows
        ]

    async def delete(self, session_id: str) -> None:
        """Delete a session (and its nodes, via ON DELETE CASCADE)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {SESSIONS_TABLE} WHERE session_id = $1", session_id
            )

    # ── Extra methods expected by phoson_cli (duck-typed, not in the ABC) ──

    async def save_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        """Update session-level metadata in place, without touching nodes."""
        pool = await self._ensure_pool()
        total_tokens = int(meta.get("total_input_tokens", 0)) + int(
            meta.get("total_output_tokens", 0)
        )
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE {SESSIONS_TABLE}
                SET total_cost = $2, total_tokens = $3, step_count = $4,
                    last_model = COALESCE($5, last_model), updated_at = now()
                WHERE session_id = $1
                """,
                session_id,
                float(meta.get("total_cost_usd", 0.0)),
                total_tokens,
                int(meta.get("step_count", 0)),
                meta.get("last_model") or None,
            )
            if result == "UPDATE 0":
                raise PhosonSessionNotFoundError(
                    f"Session {session_id} does not exist.",
                    session_id=session_id,
                )

    async def list_meta(self) -> list[SessionMeta]:
        """Alias for :meth:`list_sessions`, kept for API parity with JsonlStorage."""
        return await self.list_sessions()
