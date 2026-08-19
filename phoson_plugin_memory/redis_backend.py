"""Redis-backed short-term MemoryBackend implementation."""

from typing import Any
from dataclasses import field, dataclass

try:
    import redis.asyncio as redis_asyncio

    REDIS_AVAILABLE = True
except ImportError:
    redis_asyncio = None  # type: ignore[assignment]
    REDIS_AVAILABLE = False

from phoson_plugin_memory.backend import MemoryBackend


@dataclass
class RedisBackend(MemoryBackend):
    """Short-term memory tier backed by Redis, with per-key TTL.

    Keys are namespaced (``{namespace}:{key}``) so multiple agents/sessions
    can safely share one Redis instance without colliding.

    Args:
        url: Redis connection URL, e.g. ``redis://localhost:6379/0``.
        namespace: Key prefix scoping this backend's keys.
        default_ttl_seconds: TTL applied when ``set()`` doesn't specify one.
            None means keys never expire unless told to.
    """

    url: str = "redis://localhost:6379/0"
    namespace: str = "phoson"
    default_ttl_seconds: int | None = None

    _client: Any | None = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> Any:
        if not REDIS_AVAILABLE:
            raise ImportError(
                "redis package not installed. Install with: pip install redis "
                "or pip install 'phoson-engine-minimal[memory]'"
            )
        assert redis_asyncio is not None
        if self._client is None:
            self._client = redis_asyncio.from_url(self.url, decode_responses=True)
        return self._client

    def _namespaced(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    async def get(self, key: str) -> str | None:
        client = self._ensure_client()
        return await client.get(self._namespaced(key))

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        client = self._ensure_client()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        await client.set(self._namespaced(key), value, ex=ttl)

    async def delete(self, key: str) -> None:
        client = self._ensure_client()
        await client.delete(self._namespaced(key))

    async def list_keys(self, prefix: str = "") -> list[str]:
        client = self._ensure_client()
        pattern = f"{self._namespaced(prefix)}*"
        strip_len = len(self.namespace) + 1
        keys: list[str] = []
        async for raw_key in client.scan_iter(match=pattern):
            keys.append(raw_key[strip_len:])
        return keys

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
