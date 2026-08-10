"""Memory plugin: exposes a MemoryBackend as memory_read/memory_write tools.

Tools are built directly as AgentTool (native Phoson contract) rather than
via any StructuredTool-style wrapper, matching what Phoson-Core's
UnifiedMemory needs without pulling in LangChain.
"""

from typing import Any

from phoson_agent import Plugin, AgentTool

from .backend import MemoryBackend
from .redis_backend import RedisBackend


class MemoryPlugin(Plugin):
    """Plugin providing short-term memory tools backed by Redis.

    Configuration:
        redis_url: Redis connection URL (default ``redis://localhost:6379/0``).
        namespace: Key namespace, useful to scope memory per session/agent
            (default ``"phoson"``).
        default_ttl_seconds: TTL applied to writes that don't specify one
            (default None — no expiry).

    Postgres (long-term) and Qdrant (semantic) tiers are not implemented
    here yet; this plugin only wires the short-term Redis tier described in
    the memory backend interface (``MemoryBackend``).
    """

    def __init__(self) -> None:
        self._redis_url = "redis://localhost:6379/0"
        self._namespace = "phoson"
        self._default_ttl_seconds: int | None = None
        self.backend: MemoryBackend | None = None

    @property
    def name(self) -> str:
        return "phoson-plugin-memory"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Short-term (Redis) memory tools for Phoson Agent"

    def configure(self, config: dict[str, Any]) -> None:
        self._redis_url = config.get("redis_url", self._redis_url)
        self._namespace = config.get("namespace", self._namespace)
        self._default_ttl_seconds = config.get("default_ttl_seconds")

    def initialize(self) -> None:
        self.backend = RedisBackend(
            url=self._redis_url,
            namespace=self._namespace,
            default_ttl_seconds=self._default_ttl_seconds,
        )

    def get_tools(self) -> list[AgentTool]:
        assert self.backend is not None, "initialize() must run before get_tools()"
        backend = self.backend

        async def memory_read(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            key = args.get("key")
            if not key:
                return {"error": "Missing required field: key"}
            value = await backend.get(key)
            if value is None:
                return {"found": False, "key": key}
            return {"found": True, "key": key, "value": value}

        async def memory_write(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            key = args.get("key")
            value = args.get("value")
            if not key or value is None:
                return {"error": "Missing required field(s): key, value"}
            await backend.set(key, str(value), ttl_seconds=args.get("ttl_seconds"))
            return {"stored": True, "key": key}

        return [
            AgentTool(
                name="memory_read",
                description=(
                    "Read a value previously stored in short-term memory by key. "
                    "Returns found=False if the key doesn't exist or expired."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Memory key to read",
                        },
                    },
                    "required": ["key"],
                },
                handler=memory_read,
            ),
            AgentTool(
                name="memory_write",
                description=(
                    "Store a value in short-term memory under a key. "
                    "Optionally set ttl_seconds to expire it automatically."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Memory key"},
                        "value": {"type": "string", "description": "Value to store"},
                        "ttl_seconds": {
                            "type": "integer",
                            "description": "Optional TTL override, in seconds",
                        },
                    },
                    "required": ["key", "value"],
                },
                handler=memory_write,
            ),
        ]

    def cleanup(self) -> None:
        # backend.close() is async (redis.asyncio); callers needing a clean
        # shutdown should `await plugin.backend.close()` directly instead of
        # relying on this sync hook.
        self.backend = None


def create_plugin() -> MemoryPlugin:
    """Factory function to create a memory plugin instance."""
    return MemoryPlugin()
