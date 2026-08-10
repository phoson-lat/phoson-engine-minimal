"""Integration tests for RedisBackend against a real Redis instance.

Requires the ``redis-test`` service from ``docker-compose.test.yml``:

    docker compose -f docker-compose.test.yml up -d redis-test
    pytest tests/phoson_plugin_memory -q

Skipped automatically (not failed) when redis isn't installed or the
service isn't reachable.
"""

import os
import asyncio

import pytest

try:
    import redis.exceptions

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

if REDIS_AVAILABLE:
    from phoson_plugin_memory.redis_backend import RedisBackend

URL = os.environ.get("PHOSON_TEST_REDIS_URL", "redis://localhost:56379/0")

pytestmark = pytest.mark.skipif(
    not REDIS_AVAILABLE, reason="redis not installed (pip install redis)"
)


@pytest.fixture
async def backend():
    store = RedisBackend(url=URL, namespace="phoson-test")
    try:
        client = store._ensure_client()
        await client.ping()
    except (OSError, redis.exceptions.RedisError) as exc:
        pytest.skip(
            f"Redis not reachable at {URL}: {exc}. Start it with: "
            "docker compose -f docker-compose.test.yml up -d redis-test"
        )

    yield store

    for key in await store.list_keys():
        await store.delete(key)
    await store.close()


@pytest.mark.asyncio
async def test_set_and_get_roundtrip(backend):
    await backend.set("greeting", "hello")

    value = await backend.get("greeting")

    assert value == "hello"


@pytest.mark.asyncio
async def test_get_missing_key_returns_none(backend):
    value = await backend.get("does-not-exist")
    assert value is None


@pytest.mark.asyncio
async def test_ttl_expires_key(backend):
    await backend.set("short-lived", "value", ttl_seconds=1)

    assert await backend.get("short-lived") == "value"
    await asyncio.sleep(1.5)
    assert await backend.get("short-lived") is None


@pytest.mark.asyncio
async def test_delete_removes_key(backend):
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
async def test_namespaces_isolate_keys(backend):
    other = RedisBackend(url=URL, namespace="phoson-test-other")
    await backend.set("shared-key", "from-backend")
    await other.set("shared-key", "from-other")

    assert await backend.get("shared-key") == "from-backend"
    assert await other.get("shared-key") == "from-other"

    await other.delete("shared-key")
    await other.close()
