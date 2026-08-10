"""Memory plugin: exposes a MemoryBackend as memory_read/memory_write tools.

Tools are built directly as AgentTool (native Phoson contract) rather than
via any StructuredTool-style wrapper, matching what Phoson-Core's
UnifiedMemory needs without pulling in LangChain.
"""

import asyncio
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
            interface, so the tools behave identically regardless of which
            one is selected.
        tool_prefix: Prepended to every tool name (default ``""``). Set
            this when combining two ``MemoryPlugin`` instances in the same
            agent (e.g. Redis + Postgres side by side) — without a prefix,
            both would register identically-named tools and the second one
            loaded would silently shadow the first.
        namespace: Key namespace, useful to scope memory per session/agent
            (default ``"phoson"``).
        default_ttl_seconds: TTL applied to writes that don't specify one
            (default None — no expiry).
        redis_url: Redis connection URL, only used when ``backend="redis"``
            (default ``redis://localhost:6379/0``).
        dsn: PostgreSQL connection string, required when
            ``backend="postgres"``.
        purge_interval_seconds: Only used when ``backend="postgres"``
            (Redis expires keys natively). If set, a background task calls
            ``PostgresBackend.purge_expired()`` on this interval so expired
            rows don't accumulate forever. Started lazily on first tool
            call (needs a running event loop) and cancelled on cleanup.

    A semantic (Qdrant) tier is exposed by the separate
    ``SemanticMemoryPlugin`` — similarity search needs a different tool
    contract than exact-key lookup, not just another backend here.
    """

    def __init__(self) -> None:
        self._backend_kind = "redis"
        self._redis_url = "redis://localhost:6379/0"
        self._dsn: str | None = None
        self._namespace = "phoson"
        self._default_ttl_seconds: int | None = None
        self._tool_prefix = ""
        self._purge_interval_seconds: float | None = None
        self.backend: MemoryBackend | None = None
        self._purge_task: asyncio.Task | None = None

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
        self._tool_prefix = config.get("tool_prefix", self._tool_prefix)
        self._purge_interval_seconds = config.get("purge_interval_seconds")

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

    def _tool_name(self, base: str) -> str:
        return f"{self._tool_prefix}{base}"

    def _ensure_purge_task_started(self) -> None:
        """Lazily start the background purge loop on first tool use.

        Needs a running event loop, which ``initialize()`` (called
        synchronously by the plugin loader) can't guarantee — but any
        actual tool call always happens inside the agent's running loop.
        """
        if (
            self._purge_task is not None
            or self._purge_interval_seconds is None
            or not isinstance(self.backend, PostgresBackend)
        ):
            return

        backend = self.backend
        interval = self._purge_interval_seconds

        async def _purge_loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await backend.purge_expired()
                except Exception:
                    # Best-effort background maintenance: a transient DB
                    # hiccup shouldn't kill the loop or the agent.
                    pass

        self._purge_task = asyncio.get_running_loop().create_task(_purge_loop())

    def get_tools(self) -> list[AgentTool]:
        assert self.backend is not None, "initialize() must run before get_tools()"
        backend = self.backend

        async def memory_read(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            self._ensure_purge_task_started()
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
            self._ensure_purge_task_started()
            key = args.get("key")
            value = args.get("value")
            if not key or value is None:
                return {"error": "Missing required field(s): key, value"}
            await backend.set(key, str(value), ttl_seconds=args.get("ttl_seconds"))
            return {"stored": True, "key": key}

        async def memory_delete(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            key = args.get("key")
            if not key:
                return {"error": "Missing required field: key"}
            await backend.delete(key)
            return {"deleted": True, "key": key}

        async def memory_list(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            prefix = args.get("prefix", "")
            keys = await backend.list_keys(prefix)
            return {"keys": keys}

        return [
            AgentTool(
                name=self._tool_name("memory_read"),
                description=(
                    "Read a value previously stored in memory by key. "
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
                name=self._tool_name("memory_write"),
                description=(
                    "Store a value in memory under a key. "
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
            AgentTool(
                name=self._tool_name("memory_delete"),
                description="Delete a value stored in memory by key.",
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Memory key to delete",
                        },
                    },
                    "required": ["key"],
                },
                handler=memory_delete,
            ),
            AgentTool(
                name=self._tool_name("memory_list"),
                description="List memory keys, optionally filtered by prefix.",
                parameters={
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": "Only list keys starting with this prefix",
                        },
                    },
                },
                handler=memory_list,
            ),
        ]

    def cleanup(self) -> None:
        # backend.close() is async (redis.asyncio/asyncpg); callers needing
        # a clean shutdown should `await plugin.aclose()` directly instead
        # of relying on this sync hook.
        if self._purge_task is not None:
            self._purge_task.cancel()
            self._purge_task = None
        self.backend = None

    async def aclose(self) -> None:
        """Async, awaitable teardown: cancels the purge task and closes the backend."""
        if self._purge_task is not None:
            self._purge_task.cancel()
            self._purge_task = None
        if self.backend is not None:
            await self.backend.close()
        self.backend = None


def create_plugin() -> MemoryPlugin:
    """Factory function to create a memory plugin instance."""
    return MemoryPlugin()
