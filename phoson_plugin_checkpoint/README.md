# Phoson Checkpoint Plugin

PostgreSQL-backed session persistence (`SessionStorage`) with its own schema (`phoson_checkpoint_sessions`, `phoson_checkpoint_nodes`). It does not depend on any host-application tables — it is safe to point it at the same database used by Phoson-Core or any other app.

## Installation

```bash
pip install "phoson-engine-minimal[checkpoint]"  # installs asyncpg
```

## Direct usage (without the plugin system)

```python
from phoson_plugin_checkpoint import PostgresStorage

storage = PostgresStorage(dsn="postgresql://phoson:phoson@localhost/phoson")
await storage.save(tree)
tree = await storage.load(session_id)
sessions = await storage.list_sessions()
await storage.close()
```

## Usage as a plugin

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-checkpoint",
            "config": {"dsn": "postgresql://phoson:phoson@localhost/phoson"},
        }
    ],
)

checkpoint = next(p for p in engine._loaded_plugins if p.name == "phoson-plugin-checkpoint")
storage = checkpoint.storage  # PostgresStorage, ready to use
```

## Schema

- `phoson_checkpoint_sessions`: one row per session (metadata: cost, tokens, step_count, last_model).
- `phoson_checkpoint_nodes`: one row per conversation-tree node, `ON DELETE CASCADE` from sessions.

Tables are created automatically (`CREATE TABLE IF NOT EXISTS`) on first use.

## Integration tests

Require a real Postgres:

```bash
docker compose -f docker-compose.test.yml up -d postgres-test
pytest tests/phoson_plugin_checkpoint -q
```

If Postgres is not running, the tests skip (do not fail) automatically.
