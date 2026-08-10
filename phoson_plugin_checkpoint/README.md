# Phoson Checkpoint Plugin

Session persistence (`SessionStorage`) respaldada en PostgreSQL, con su propio esquema (`phoson_checkpoint_sessions`, `phoson_checkpoint_nodes`). No depende de tablas de ninguna aplicación host — es seguro apuntarlo a la misma base de datos que usa Phoson-Core u otra app.

## Instalación

```bash
pip install "phoson-engine-minimal[checkpoint]"  # instala asyncpg
```

## Uso directo (sin pasar por el sistema de plugins)

```python
from phoson_plugin_checkpoint import PostgresStorage

storage = PostgresStorage(dsn="postgresql://phoson:phoson@localhost/phoson")
await storage.save(tree)
tree = await storage.load(session_id)
sessions = await storage.list_sessions()
await storage.close()
```

## Uso como Plugin

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

## Esquema

- `phoson_checkpoint_sessions`: una fila por sesión (metadata: costo, tokens, step_count, last_model).
- `phoson_checkpoint_nodes`: una fila por nodo del árbol de conversación, `ON DELETE CASCADE` desde sessions.

Las tablas se crean automáticamente (`CREATE TABLE IF NOT EXISTS`) en la primera operación.

## Tests de integración

Requieren un Postgres real:

```bash
docker compose -f docker-compose.test.yml up -d postgres-test
pytest tests/phoson_plugin_checkpoint -q
```

Si Postgres no está corriendo, los tests se saltan (no fallan) automáticamente.
