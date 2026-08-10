"""Unit tests for MemoryPlugin's tool contract, decoupled from Redis.

Exercises memory_read/memory_write against a trivial in-process
MemoryBackend implementation, so these tests don't need a running Redis.
"""

import pytest

from phoson_plugin_memory.plugin import MemoryPlugin
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


def test_get_tools_returns_read_and_write():
    plugin = MemoryPlugin()
    plugin.backend = FakeBackend()

    tools = plugin.get_tools()
    names = {t.name for t in tools}

    assert names == {"memory_read", "memory_write"}


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


def test_get_tools_before_initialize_raises():
    plugin = MemoryPlugin()
    with pytest.raises(AssertionError):
        plugin.get_tools()


def test_cleanup_clears_backend(plugin):
    plugin.cleanup()
    assert plugin.backend is None
