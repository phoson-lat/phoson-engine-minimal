"""
Phoson Memory Plugin

Short-term (Redis, TTL-based) and long-term (Postgres) memory for Phoson
Agent, exposed as memory_read/memory_write AgentTools. A semantic (Qdrant)
tier would need a different interface (similarity search, not exact key
lookup) and is left for when there's a concrete need for it.
"""

from .plugin import MemoryPlugin
from .backend import MemoryBackend
from .redis_backend import RedisBackend
from .postgres_backend import PostgresBackend

__version__ = "0.1.0"

# Export plugin instance (package-loader convention, see docs/plugins.md)
plugin = MemoryPlugin()

__all__ = [
    "MemoryPlugin",
    "MemoryBackend",
    "RedisBackend",
    "PostgresBackend",
    "plugin",
]
