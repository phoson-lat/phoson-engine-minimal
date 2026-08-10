"""
Phoson Memory Plugin

Short-term (Redis, TTL-based) and long-term (Postgres) memory for Phoson
Agent, exposed as memory_read/memory_write AgentTools — plus a separate
semantic (Qdrant) tier exposed as memory_remember/memory_recall, since
similarity search needs a different tool contract than exact-key lookup.
The semantic tier requires an injected embed_fn; no embedding provider is
bundled here.
"""

from .plugin import MemoryPlugin
from .backend import MemoryBackend
from .redis_backend import RedisBackend
from .qdrant_backend import QdrantBackend
from .semantic_plugin import SemanticMemoryPlugin
from .postgres_backend import PostgresBackend
from .semantic_backend import SemanticMatch, SemanticMemoryBackend

__version__ = "0.1.0"

# Export plugin instance (package-loader convention, see docs/plugins.md)
plugin = MemoryPlugin()

__all__ = [
    "MemoryPlugin",
    "MemoryBackend",
    "RedisBackend",
    "PostgresBackend",
    "SemanticMemoryPlugin",
    "SemanticMemoryBackend",
    "SemanticMatch",
    "QdrantBackend",
    "plugin",
]
