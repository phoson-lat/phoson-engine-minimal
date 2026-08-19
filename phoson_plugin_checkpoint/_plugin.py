"""Checkpoint plugin: wires PostgresStorage into the Plugin lifecycle.

This plugin does not add tools or middlewares — its only job is to own a
:class:`PostgresStorage` instance, configured from plugin config, so it can
be constructed the same way as any other plugin (``plugins=[...]``) while
the resulting ``.storage`` is what actually gets handed to whatever runs
the session-persistence side of the agent (e.g. Phoson-Core).
"""

from typing import Any

from phoson_agent import Plugin

from .storage import PostgresStorage


class CheckpointPlugin(Plugin):
    """Plugin exposing a Postgres-backed :class:`SessionStorage`.

    Configuration:
        dsn: PostgreSQL connection string (required).
        pool_min_size: Minimum pool connections (default 1).
        pool_max_size: Maximum pool connections (default 10).

    Example:
        plugin = CheckpointPlugin()
        plugin.configure({"dsn": "postgresql://phoson:phoson@localhost/phoson"})
        plugin.initialize()
        await plugin.storage.save(tree)
    """

    def __init__(self) -> None:
        self._dsn: str | None = None
        self._pool_min_size = 1
        self._pool_max_size = 10
        self.storage: PostgresStorage | None = None

    @property
    def name(self) -> str:
        return "phoson-plugin-checkpoint"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Postgres-backed session checkpoint storage for Phoson Agent"

    def configure(self, config: dict[str, Any]) -> None:
        dsn = config.get("dsn")
        if not dsn:
            raise ValueError("phoson-plugin-checkpoint requires a 'dsn' config value")
        self._dsn = dsn
        self._pool_min_size = int(config.get("pool_min_size", 1))
        self._pool_max_size = int(config.get("pool_max_size", 10))

    def initialize(self) -> None:
        assert self._dsn is not None, "configure() must run before initialize()"
        self.storage = PostgresStorage(
            dsn=self._dsn,
            pool_min_size=self._pool_min_size,
            pool_max_size=self._pool_max_size,
        )

    def cleanup(self) -> None:
        # Pool teardown is async (asyncpg.Pool.close()); callers that need
        # a clean shutdown should `await plugin.storage.close()` directly
        # before/instead of relying on the sync cleanup() hook.
        self.storage = None


def create_plugin() -> CheckpointPlugin:
    """Factory function to create a checkpoint plugin instance."""
    return CheckpointPlugin()
