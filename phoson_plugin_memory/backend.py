"""Backend interface for phoson-plugin-memory.

Mirrors the shape of Phoson-Core's ``core/memory/unified.py::UnifiedMemory``
short-term tier, decoupled from Core: a plain key/value store with optional
TTL. Long-term (Postgres) and semantic (Qdrant) tiers implement the same
interface and get added later without touching this contract or the plugin
that consumes it.
"""

from abc import ABC, abstractmethod


class MemoryBackend(ABC):
    """Abstract backend for short-term (and, later, other-tier) memory.

    Implement this to plug a new storage tier into ``phoson-plugin-memory``
    without changing the plugin or its tools.
    """

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Return the stored value for ``key``, or None if absent/expired."""
        raise NotImplementedError

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store ``value`` under ``key``, expiring after ``ttl_seconds`` if given."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key`` if present. Must not raise if it's already absent."""
        raise NotImplementedError

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        """List stored keys, optionally filtered by prefix."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release any underlying connections. Safe to call multiple times."""
        raise NotImplementedError
