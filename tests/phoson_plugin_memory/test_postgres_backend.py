"""Integration tests for PostgresBackend against a real Postgres instance.

Requires the ``postgres-test`` service from ``docker-compose.test.yml``:

    docker compose -f docker-compose.test.yml up -d postgres-test
    pytest tests/phoson_plugin_memory -q

Skipped automatically (not failed) when asyncpg isn't installed or the
service isn't reachable. Shares the same Postgres instance/database as
phoson_plugin_checkpoint's tests, but its own table
(``phoson_memory_entries``), so the two never collide.
"""

import os
import asyncio

import pytest

try:
    import asyncpg

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

if ASYNCPG_AVAILABLE:
    from phoson_plugin_memory.postgres_backend import TABLE, PostgresBackend

DSN = os.environ.get(
    "PHOSON_TEST_POSTGRES_DSN",
    "postgresql://phoson:phoson@localhost:55432/phoson_test",
)

pytestmark = pytest.mark.skipif(
    not ASYNCPG_AVAILABLE, reason="asyncpg not installed (pip install asyncpg)"
)


@pytest.fixture
async def backend():
    store = PostgresBackend(dsn=DSN, namespace="phoson-memory-test")
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
        await conn.execute(f"DELETE FROM {TABLE} WHERE namespace = $1", store.namespace)
    await store.close()


@pytest.mark.asyncio
async def test_set_and_get_roundtrip(backend):
    await backend.set("greeting", "hello")

    assert await backend.get("greeting") == "hello"


@pytest.mark.asyncio
async def test_get_missing_key_returns_none(backend):
    assert await backend.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_set_overwrites_existing_value(backend):
    await backend.set("k", "first")
    await backend.set("k", "second")

    assert await backend.get("k") == "second"


@pytest.mark.asyncio
async def test_ttl_expires_entry(backend):
    await backend.set("short-lived", "value", ttl_seconds=1)

    assert await backend.get("short-lived") == "value"
    await asyncio.sleep(1.5)
    assert await backend.get("short-lived") is None


@pytest.mark.asyncio
async def test_set_without_ttl_never_expires_by_default(backend):
    await backend.set("permanent", "value")
    await asyncio.sleep(1.1)

    assert await backend.get("permanent") == "value"


@pytest.mark.asyncio
async def test_delete_removes_entry(backend):
    await backend.set("to-delete", "value")

    await backend.delete("to-delete")

    assert await backend.get("to-delete") is None


@pytest.mark.asyncio
async def test_delete_nonexistent_does_not_raise(backend):
    await backend.delete("never-existed")


@pytest.mark.asyncio
async def test_list_keys_filters_by_prefix(backend):
    await backend.set("user:1", "a")
    await backend.set("user:2", "b")
    await backend.set("session:1", "c")

    user_keys = await backend.list_keys("user:")

    assert sorted(user_keys) == ["user:1", "user:2"]


@pytest.mark.asyncio
async def test_list_keys_excludes_expired_entries(backend):
    await backend.set("still-here", "value")
    await backend.set("about-to-expire", "value", ttl_seconds=1)
    await asyncio.sleep(1.5)

    keys = await backend.list_keys()

    assert keys == ["still-here"]


@pytest.mark.asyncio
async def test_namespaces_isolate_entries(backend):
    other = PostgresBackend(dsn=DSN, namespace="phoson-memory-test-other")
    await backend.set("shared-key", "from-backend")
    await other.set("shared-key", "from-other")

    assert await backend.get("shared-key") == "from-backend"
    assert await other.get("shared-key") == "from-other"

    pool = await other._ensure_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {TABLE} WHERE namespace = $1", other.namespace)
    await other.close()


@pytest.mark.asyncio
async def test_purge_expired_removes_only_expired_rows(backend):
    await backend.set("expired-1", "v", ttl_seconds=1)
    await backend.set("expired-2", "v", ttl_seconds=1)
    await backend.set("alive", "v")
    await asyncio.sleep(1.5)

    removed = await backend.purge_expired()

    assert removed == 2
    assert await backend.get("alive") == "v"
