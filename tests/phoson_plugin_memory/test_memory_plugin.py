"""Unit tests for MemoryPlugin's tool contract, decoupled from Redis.

Exercises memory_read/memory_write against a trivial in-process
MemoryBackend implementation, so these tests don't need a running Redis.
"""

import importlib
from unittest.mock import MagicMock

import pytest

from phoson_plugin_memory import MemoryPlugin
from phoson_plugin_memory.backend import MemoryBackend


class FakeBackend(MemoryBackend):
    """Minimal in-process MemoryBackend, only for exercising the plugin/tool
    contract without a real Redis connection."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def list_keys(self, prefix: str = "") -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]

    async def close(self) -> None:
        pass


@pytest.fixture
def plugin() -> MemoryPlugin:
    p = MemoryPlugin()
    p.backend = FakeBackend()
    return p


def test_plugin_properties():
    plugin = MemoryPlugin()
    assert plugin.name == "phoson-plugin-memory"
    assert plugin.version == "0.1.0"
    assert "memory" in plugin.description.lower()


def test_configure_sets_redis_url_and_namespace():
    plugin = MemoryPlugin()
    plugin.configure(
        {
            "redis_url": "redis://example:6380/1",
            "namespace": "custom-ns",
            "default_ttl_seconds": 60,
        }
    )
    plugin.initialize()

    assert plugin.backend is not None
    assert plugin.backend.url == "redis://example:6380/1"
    assert plugin.backend.namespace == "custom-ns"
    assert plugin.backend.default_ttl_seconds == 60


def test_defaults_to_redis_backend():
    plugin = MemoryPlugin()
    plugin.configure({})
    plugin.initialize()

    assert type(plugin.backend).__name__ == "RedisBackend"


def test_configure_with_postgres_backend():
    plugin = MemoryPlugin()
    plugin.configure(
        {
            "backend": "postgres",
            "dsn": "postgresql://user:pass@example/db",
            "namespace": "custom-ns",
            "default_ttl_seconds": 30,
        }
    )
    plugin.initialize()

    assert type(plugin.backend).__name__ == "PostgresBackend"
    assert plugin.backend.dsn == "postgresql://user:pass@example/db"
    assert plugin.backend.namespace == "custom-ns"
    assert plugin.backend.default_ttl_seconds == 30


def test_configure_postgres_without_dsn_raises():
    plugin = MemoryPlugin()
    with pytest.raises(ValueError, match="dsn"):
        plugin.configure({"backend": "postgres"})


def test_configure_unsupported_backend_raises():
    plugin = MemoryPlugin()
    with pytest.raises(ValueError, match="Unsupported backend"):
        plugin.configure({"backend": "qdrant"})


def test_get_tools_returns_full_crud_set():
    plugin = MemoryPlugin()
    plugin.backend = FakeBackend()

    tools = plugin.get_tools()
    names = {t.name for t in tools}

    assert names == {"memory_read", "memory_write", "memory_delete", "memory_list"}


def test_tool_prefix_avoids_collisions_between_instances():
    redis_like = MemoryPlugin()
    redis_like.backend = FakeBackend()

    postgres_like = MemoryPlugin()
    postgres_like.configure({"tool_prefix": "longterm_"})
    postgres_like.backend = FakeBackend()

    redis_names = {t.name for t in redis_like.get_tools()}
    postgres_names = {t.name for t in postgres_like.get_tools()}

    assert redis_names.isdisjoint(postgres_names)
    assert postgres_names == {
        "longterm_memory_read",
        "longterm_memory_write",
        "longterm_memory_delete",
        "longterm_memory_list",
    }


@pytest.mark.asyncio
async def test_memory_write_then_read_roundtrip(plugin):
    write_tool = next(t for t in plugin.get_tools() if t.name == "memory_write")
    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")

    write_result = await write_tool.handler({"key": "foo", "value": "bar"})
    assert write_result == {"stored": True, "key": "foo"}

    read_result = await read_tool.handler({"key": "foo"})
    assert read_result == {"found": True, "key": "foo", "value": "bar"}


@pytest.mark.asyncio
async def test_memory_read_missing_key_returns_not_found(plugin):
    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")

    result = await read_tool.handler({"key": "does-not-exist"})

    assert result == {"found": False, "key": "does-not-exist"}


@pytest.mark.asyncio
async def test_memory_read_missing_key_arg_returns_error(plugin):
    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")

    result = await read_tool.handler({})

    assert "error" in result


@pytest.mark.asyncio
async def test_memory_write_missing_value_arg_returns_error(plugin):
    write_tool = next(t for t in plugin.get_tools() if t.name == "memory_write")

    result = await write_tool.handler({"key": "foo"})

    assert "error" in result


@pytest.mark.asyncio
async def test_memory_delete_removes_key(plugin):
    write_tool = next(t for t in plugin.get_tools() if t.name == "memory_write")
    delete_tool = next(t for t in plugin.get_tools() if t.name == "memory_delete")
    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")

    await write_tool.handler({"key": "foo", "value": "bar"})
    result = await delete_tool.handler({"key": "foo"})
    assert result == {"deleted": True, "key": "foo"}

    read_result = await read_tool.handler({"key": "foo"})
    assert read_result == {"found": False, "key": "foo"}


@pytest.mark.asyncio
async def test_memory_delete_missing_key_arg_returns_error(plugin):
    delete_tool = next(t for t in plugin.get_tools() if t.name == "memory_delete")

    result = await delete_tool.handler({})

    assert "error" in result


@pytest.mark.asyncio
async def test_memory_list_filters_by_prefix(plugin):
    write_tool = next(t for t in plugin.get_tools() if t.name == "memory_write")
    list_tool = next(t for t in plugin.get_tools() if t.name == "memory_list")

    await write_tool.handler({"key": "user:1", "value": "a"})
    await write_tool.handler({"key": "user:2", "value": "b"})
    await write_tool.handler({"key": "session:1", "value": "c"})

    result = await list_tool.handler({"prefix": "user:"})

    assert sorted(result["keys"]) == ["user:1", "user:2"]


@pytest.mark.asyncio
async def test_memory_list_without_prefix_returns_all_keys(plugin):
    write_tool = next(t for t in plugin.get_tools() if t.name == "memory_write")
    list_tool = next(t for t in plugin.get_tools() if t.name == "memory_list")

    await write_tool.handler({"key": "a", "value": "1"})
    await write_tool.handler({"key": "b", "value": "2"})

    result = await list_tool.handler({})

    assert sorted(result["keys"]) == ["a", "b"]


def test_get_tools_before_initialize_raises():
    plugin = MemoryPlugin()
    with pytest.raises(AssertionError):
        plugin.get_tools()


def test_cleanup_clears_backend(plugin):
    plugin.cleanup()
    assert plugin.backend is None


def test_cleanup_cancels_purge_task(plugin):
    fake_task = MagicMock()
    plugin._purge_task = fake_task

    plugin.cleanup()

    fake_task.cancel.assert_called_once()
    assert plugin._purge_task is None


@pytest.mark.asyncio
async def test_aclose_cancels_purge_task_and_closes_backend(plugin):
    fake_task = MagicMock()
    plugin._purge_task = fake_task

    await plugin.aclose()

    fake_task.cancel.assert_called_once()
    assert plugin._purge_task is None
    assert plugin.backend is None


@pytest.mark.asyncio
async def test_purge_task_not_started_without_interval_configured(plugin):
    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")

    await read_tool.handler({"key": "anything"})

    assert plugin._purge_task is None


@pytest.mark.asyncio
async def test_purge_task_not_started_for_non_postgres_backend():
    plugin = MemoryPlugin()
    plugin.configure({"purge_interval_seconds": 60})
    plugin.backend = FakeBackend()  # not a PostgresBackend instance

    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")
    await read_tool.handler({"key": "anything"})

    assert plugin._purge_task is None


@pytest.mark.asyncio
async def test_purge_task_starts_lazily_for_postgres_backend(monkeypatch):
    plugin = MemoryPlugin()
    plugin.configure(
        {"backend": "postgres", "dsn": "postgresql://x", "purge_interval_seconds": 60}
    )
    fake_postgres_backend = FakeBackend()
    # The plugin module lives in `_plugin.py` (leading underscore) precisely
    # so the package-level `plugin = MemoryPlugin()` instance does NOT shadow
    # the submodule attribute — see issue #27. Regular module imports work.
    plugin_module = importlib.import_module("phoson_plugin_memory._plugin")
    monkeypatch.setattr(plugin_module, "PostgresBackend", type(fake_postgres_backend))
    plugin.backend = fake_postgres_backend

    read_tool = next(t for t in plugin.get_tools() if t.name == "memory_read")
    await read_tool.handler({"key": "anything"})

    try:
        assert plugin._purge_task is not None
        assert not plugin._purge_task.done()
    finally:
        plugin._purge_task.cancel()
