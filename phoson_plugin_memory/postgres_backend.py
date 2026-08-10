"""Postgres-backed long-term MemoryBackend implementation.

Same MemoryBackend interface as RedisBackend, own schema
(``phoson_memory_entries``), no dependency on any host-application table.

Unlike Redis, Postgres has no native per-key expiry: TTL is enforced by
filtering ``expires_at`` on reads. Expired rows are skipped but not
deleted automatically — call :meth:`PostgresBackend.purge_expired`
periodically (e.g. from a cron/background task) to reclaim them.
"""

import asyncio
from typing import Any
from dataclasses import field, dataclass

try:
    import asyncpg

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

from phoson_plugin_memory.backend import MemoryBackend

TABLE = "phoson_memory_entries"

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS phoson_memory_entries_expires_at_idx
    ON {TABLE} (expires_at) WHERE expires_at IS NOT NULL;
"""


@dataclass
class PostgresBackend(MemoryBackend):
    """Long-term memory tier backed by Postgres.

    Args:
        dsn: PostgreSQL connection string.
        namespace: Logical scope for keys, so multiple agents/sessions can
            share one database/table without colliding.
        default_ttl_seconds: TTL applied when ``set()`` doesn't specify one.
            None means entries never expire unless told to.
        pool_min_size: Minimum pool connections.
        pool_max_size: Maximum pool connections.
    """

    dsn: str
    namespace: str = "phoson"
    default_ttl_seconds: int | None = None
    pool_min_size: int = 1
    pool_max_size: int = 10

    _pool: Any = field(default=None, init=False, repr=False)
    _pool_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    async def _ensure_pool(self) -> Any:
        if not ASYNCPG_AVAILABLE:
            raise ImportError(
                "asyncpg not installed. Install with: pip install asyncpg "
                "or pip install 'phoson-engine-minimal[memory]'"
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
                self._pool = pool
        return self._pool

    async def get(self, key: str) -> str | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT value FROM {TABLE}
                WHERE namespace = $1 AND key = $2
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                self.namespace,
                key,
            )
        return row["value"] if row is not None else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        pool = await self._ensure_pool()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {TABLE} (namespace, key, value, expires_at)
                VALUES ($1, $2, $3, CASE WHEN $4::int IS NULL THEN NULL
                                         ELSE now() + make_interval(secs => $4) END)
                ON CONFLICT (namespace, key) DO UPDATE SET
                    value = EXCLUDED.value,
                    expires_at = EXCLUDED.expires_at
                """,
                self.namespace,
                key,
                value,
                ttl,
            )

    async def delete(self, key: str) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {TABLE} WHERE namespace = $1 AND key = $2",
                self.namespace,
                key,
            )

    async def list_keys(self, prefix: str = "") -> list[str]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT key FROM {TABLE}
                WHERE namespace = $1 AND key LIKE $2 || '%'
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY key
                """,
                self.namespace,
                prefix,
            )
        return [row["key"] for row in rows]

    async def purge_expired(self) -> int:
        """Delete expired rows for this namespace. Returns rows removed.

        Postgres has no built-in TTL eviction (unlike Redis); call this
        periodically if you write a lot of short-lived entries.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"""
                DELETE FROM {TABLE}
                WHERE namespace = $1 AND expires_at IS NOT NULL AND expires_at <= now()
                """,
                self.namespace,
            )
        # asyncpg command tags look like "DELETE <n>"
        return int(result.split()[-1])

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
