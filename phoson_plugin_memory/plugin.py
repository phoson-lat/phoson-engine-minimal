"""Memory plugin: exposes a MemoryBackend as memory_read/memory_write tools.

Tools are built directly as AgentTool (native Phoson contract) rather than
via any StructuredTool-style wrapper, matching what Phoson-Core's
UnifiedMemory needs without pulling in LangChain.
"""

from typing import Any

from phoson_agent import Plugin, AgentTool

from .backend import MemoryBackend
from .redis_backend import RedisBackend
from .postgres_backend import PostgresBackend

_SUPPORTED_BACKENDS = ("redis", "postgres")


class MemoryPlugin(Plugin):
    """Plugin providing memory tools backed by a configurable MemoryBackend.

    Configuration:
        backend: ``"redis"`` (default, short-term tier) or ``"postgres"``
            (long-term tier). Both implement the same ``MemoryBackend``
            interface, so ``memory_read``/``memory_write`` behave
            identically regardless of which one is selected.
        namespace: Key namespace, useful to scope memory per session/agent
            (default ``"phoson"``).
        default_ttl_seconds: TTL applied to writes that don't specify one
            (default None — no expiry).
        redis_url: Redis connection URL, only used when ``backend="redis"``
            (default ``redis://localhost:6379/0``).
        dsn: PostgreSQL connection string, required when
            ``backend="postgres"``.

    A semantic (Qdrant) tier is not implemented — it doesn't fit this
    exact-key-lookup interface (similarity search needs a different
    ``search(query, top_k)`` shape) and is left for when there's a concrete
    need for it.
    """

    def __init__(self) -> None:
        self._backend_kind = "redis"
        self._redis_url = "redis://localhost:6379/0"
        self._dsn: str | None = None
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
        return "Memory tools (Redis short-term / Postgres long-term) for Phoson Agent"

    def configure(self, config: dict[str, Any]) -> None:
        backend_kind = config.get("backend", self._backend_kind)
        if backend_kind not in _SUPPORTED_BACKENDS:
            raise ValueError(
                f"Unsupported backend '{backend_kind}'. "
                f"Supported: {', '.join(_SUPPORTED_BACKENDS)}"
            )
        self._backend_kind = backend_kind
        self._redis_url = config.get("redis_url", self._redis_url)
        self._dsn = config.get("dsn", self._dsn)
        self._namespace = config.get("namespace", self._namespace)
        self._default_ttl_seconds = config.get("default_ttl_seconds")

        if self._backend_kind == "postgres" and not self._dsn:
            raise ValueError(
                "phoson-plugin-memory with backend='postgres' requires a 'dsn'"
            )

    def initialize(self) -> None:
        if self._backend_kind == "postgres":
            assert self._dsn is not None  # enforced in configure()
            self.backend = PostgresBackend(
                dsn=self._dsn,
                namespace=self._namespace,
                default_ttl_seconds=self._default_ttl_seconds,
            )
        else:
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
