# Phoson Memory Plugin

Memory for Phoson Agent, exposed as native tools (`AgentTool`, not `StructuredTool`). It is the extracted form of Phoson-Core's `core/memory/unified.py::UnifiedMemory`, decoupled from Core, with **two plugins** depending on the access pattern you need:

- **`MemoryPlugin`** (`memory_read`/`memory_write`, exact key lookup) — two interchangeable tiers behind the same `MemoryBackend` interface:
  - **Redis** (short-term, with TTL) — default.
  - **Postgres** (long-term) — same TTL, but enforced at read time (see below).
- **`SemanticMemoryPlugin`** (`memory_remember`/`memory_recall`, similarity search) — Qdrant tier. Deliberately a different interface: `MemoryBackend` is "give me the value for this exact key"; semantic search is "give me the N entries most similar to this text" — they do not fit the same tool contract.

## Installation

```bash
pip install "phoson-engine-minimal[memory]"  # installs redis + asyncpg + qdrant-client
```

## Usage as a plugin

### Redis tier (default)

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-memory",
            "config": {
                "redis_url": "redis://localhost:6379/0",
                "namespace": "my-agent",       # optional, key scope
                "default_ttl_seconds": 3600,   # optional
            },
        }
    ],
)
```

### Postgres tier

```python
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-memory",
            "config": {
                "backend": "postgres",
                "dsn": "postgresql://phoson:phoson@localhost/phoson",
                "namespace": "my-agent",
            },
        }
    ],
)
```

The model can then call `memory_write(key, value, ttl_seconds=...)`, `memory_read(key)`, `memory_delete(key)` and `memory_list(prefix=...)` like any other tool — the behavior is identical regardless of the chosen backend.

### Combining Redis + Postgres in the same agent

Each `MemoryPlugin` instance registers tools with the same names by default (`memory_read`, `memory_write`, ...). If you add two instances (e.g. short-term + long-term at the same time), the second silently overrides the first in `AgentEngine` — use `tool_prefix` to avoid that:

```python
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {"name": "phoson-plugin-memory", "config": {"backend": "redis"}},
        {
            "name": "phoson-plugin-memory",
            "config": {
                "backend": "postgres",
                "dsn": "postgresql://phoson:phoson@localhost/phoson",
                "tool_prefix": "longterm_",
            },
        },
    ],
)
```

The model sees `memory_read`/`memory_write`/... (Redis) and `longterm_memory_read`/`longterm_memory_write`/... (Postgres) as separate tools.

### Auto-purge for the Postgres tier

Only Redis expires keys on its own; Postgres does not. Set `purge_interval_seconds` so a background task calls `purge_expired()` automatically:

```python
{
    "name": "phoson-plugin-memory",
    "config": {
        "backend": "postgres",
        "dsn": "...",
        "purge_interval_seconds": 300,  # every 5 minutes
    },
}
```

The task starts on its own (lazy, on the first `memory_read`/`memory_write` call — it needs the agent loop already running) and is cancelled in `cleanup()`/`aclose()`.

### Qdrant tier (semantic) — separate plugin

`embed_fn` (text -> vector) has no default and is not JSON-serializable, so it is passed as a Python object — to the constructor, or via `config["embed_fn"]` if you use a dict spec:

```python
from phoson_plugin_memory import SemanticMemoryPlugin

def my_embed_fn(text: str) -> list[float]:
    ...  # your embedding provider/model — not included

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        SemanticMemoryPlugin(
            embed_fn=my_embed_fn,
            url="http://localhost:6333",
            namespace="my-agent",
        )
    ],
)
```

The model calls `memory_remember(key, text, metadata=...)` to store, `memory_recall(query, top_k=5)` to search by meaning (returns a list of `{key, text, score, metadata}` sorted by similarity) and `memory_forget(key)` to delete. `tool_prefix` works the same as in `MemoryPlugin` if you need more than one instance (e.g. two collections).

## Direct backend usage

```python
from phoson_plugin_memory import RedisBackend, PostgresBackend

redis_backend = RedisBackend(url="redis://localhost:6379/0", namespace="my-agent")
await redis_backend.set("user_name", "Abel", ttl_seconds=3600)
await redis_backend.close()

pg_backend = PostgresBackend(dsn="postgresql://phoson:phoson@localhost/phoson", namespace="my-agent")
await pg_backend.set("user_name", "Abel")
await pg_backend.close()
```

```python
from phoson_plugin_memory import QdrantBackend

qdrant_backend = QdrantBackend(embed_fn=my_embed_fn, url="http://localhost:6333")
await qdrant_backend.upsert("fact-1", "the user prefers dark mode")
matches = await qdrant_backend.search("does the user like light or dark UI?", top_k=3)
await qdrant_backend.close()
```

## TTL in Postgres

Unlike Redis, Postgres does not expire keys automatically. `PostgresBackend` filters by `expires_at` on every read (`get`/`list_keys`), so an expired value is never returned — but the row stays in the table until someone deletes it. Call `await backend.purge_expired()` periodically (cron, background task) if you write many short-TTL entries, to avoid unbounded garbage accumulation.

## Qdrant: point IDs and namespaces

Qdrant only accepts integers or UUIDs as point IDs (an arbitrary string is rejected with 400) — `QdrantBackend` derives a deterministic UUID5 from `namespace:key` and stores the original key in the payload. The collection is created on the first `upsert()`, with the vector size inferred from the first embedding. Multiple namespaces can share the same collection: the namespace filter is applied in both `search()` and `delete()`.

## Extending with another backend

Implement `MemoryBackend` (`get`/`set`/`delete`/`list_keys`/`close`) and pass it directly to `MemoryPlugin`, or `SemanticMemoryBackend` (`upsert`/`search`/`delete`/`close`) for `SemanticMemoryPlugin` — you do not need to subclass `RedisBackend`/`PostgresBackend`/`QdrantBackend`.

## Integration tests

Require real Redis, Postgres and/or Qdrant:

```bash
docker compose -f docker-compose.test.yml up -d redis-test postgres-test qdrant-test
pytest tests/phoson_plugin_memory -q
```

If the corresponding service is not running, those integration tests skip (do not fail); the unit tests for `MemoryPlugin`/`SemanticMemoryPlugin` always run because they use a fake backend.
