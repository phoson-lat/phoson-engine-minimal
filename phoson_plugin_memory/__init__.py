"""
Phoson Memory Plugin

Short-term memory (Redis, TTL-based) for Phoson Agent, exposed as
memory_read/memory_write AgentTools. Postgres (long-term) and Qdrant
(semantic) tiers implement the same MemoryBackend interface and slot in
later without changing this plugin.
"""

from .plugin import MemoryPlugin
from .backend import MemoryBackend
from .redis_backend import RedisBackend

__version__ = "0.1.0"

# Export plugin instance (package-loader convention, see docs/plugins.md)
plugin = MemoryPlugin()

__all__ = ["MemoryPlugin", "MemoryBackend", "RedisBackend", "plugin"]
