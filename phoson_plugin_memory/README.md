# Phoson Memory Plugin

Memoria para Phoson Agent, expuesta como tools nativas (`AgentTool`, no `StructuredTool`). Es la extracción de la forma de `core/memory/unified.py::UnifiedMemory` de Phoson-Core, desacoplada de Core, con **dos plugins** según el tipo de acceso que necesitas:

- **`MemoryPlugin`** (`memory_read`/`memory_write`, lookup exacto por key) — dos tiers intercambiables detrás de la misma interfaz `MemoryBackend`:
  - **Redis** (corto plazo, con TTL) — default.
  - **Postgres** (largo plazo) — mismo TTL, pero enforced a nivel de lectura (ver abajo).
- **`SemanticMemoryPlugin`** (`memory_remember`/`memory_recall`, búsqueda por similitud) — tier Qdrant. Interfaz distinta a propósito: `MemoryBackend` es "dame el valor de esta key exacta"; búsqueda semántica es "dame las N entradas más parecidas a este texto", no encajan en el mismo contrato de tool.

## Instalación

```bash
pip install "phoson-engine-minimal[memory]"  # instala redis + asyncpg + qdrant-client
```

## Uso como Plugin

### Tier Redis (default)

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
                "namespace": "my-agent",       # opcional, scope de las keys
                "default_ttl_seconds": 3600,   # opcional
            },
        }
    ],
)
```

### Tier Postgres

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

El modelo puede entonces llamar `memory_write(key, value, ttl_seconds=...)`, `memory_read(key)`, `memory_delete(key)` y `memory_list(prefix=...)` como cualquier otra tool — el comportamiento es idéntico sin importar el backend elegido.

### Combinar Redis + Postgres en el mismo agente

Cada instancia de `MemoryPlugin` registra tools con el mismo nombre por default (`memory_read`, `memory_write`, ...). Si agregas dos instancias (p.ej. corto plazo + largo plazo a la vez), la segunda pisa silenciosamente a la primera en `AgentEngine` — usa `tool_prefix` para evitarlo:

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

El modelo ve `memory_read`/`memory_write`/... (Redis) y `longterm_memory_read`/`longterm_memory_write`/... (Postgres) como tools separadas.

### Auto-purge para el tier Postgres

Redis expira keys solo; Postgres no. Configura `purge_interval_seconds` para que un background task llame `purge_expired()` automáticamente:

```python
{
    "name": "phoson-plugin-memory",
    "config": {
        "backend": "postgres",
        "dsn": "...",
        "purge_interval_seconds": 300,  # cada 5 minutos
    },
}
```

El task arranca solo (lazy, en la primera llamada a `memory_read`/`memory_write` — necesita el loop del agente ya corriendo) y se cancela en `cleanup()`/`aclose()`.

### Tier Qdrant (semántico) — plugin separado

`embed_fn` (texto -> vector) no tiene default y no es JSON-serializable, así que se pasa como objeto Python — al constructor o vía `config["embed_fn"]` si usas un spec con dict:

```python
from phoson_plugin_memory import SemanticMemoryPlugin

def my_embed_fn(text: str) -> list[float]:
    ...  # tu proveedor/modelo de embeddings — no viene incluido

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

El modelo llama `memory_remember(key, text, metadata=...)` para guardar, `memory_recall(query, top_k=5)` para buscar por significado (devuelve una lista de `{key, text, score, metadata}` ordenada por similitud) y `memory_forget(key)` para borrar. `tool_prefix` funciona igual que en `MemoryPlugin` si necesitas más de una instancia (p.ej. dos colecciones).

## Uso directo del backend

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

## TTL en Postgres

A diferencia de Redis, Postgres no expira keys automáticamente. `PostgresBackend` filtra por `expires_at` en cada lectura (`get`/`list_keys`), así que un valor expirado nunca se devuelve — pero la fila queda en la tabla hasta que alguien la borre. Llama `await backend.purge_expired()` periódicamente (cron, tarea de background) si escribes muchas entradas con TTL corto, para no acumular basura sin límite.

## Qdrant: IDs de punto y namespaces

Qdrant solo acepta enteros o UUID como ID de punto (un string arbitrario se rechaza con 400) — `QdrantBackend` deriva un UUID5 determinista de `namespace:key` y guarda la key original en el payload. La colección se crea sola en el primer `upsert()`, con el tamaño de vector inferido del primer embedding. Varios namespaces pueden compartir una misma colección: el filtro de namespace se aplica tanto en `search()` como en `delete()`.

## Extender con otro backend

Implementa `MemoryBackend` (`get`/`set`/`delete`/`list_keys`/`close`) y pásalo directamente a `MemoryPlugin`, o `SemanticMemoryBackend` (`upsert`/`search`/`delete`/`close`) para `SemanticMemoryPlugin` — no necesitas heredar de `RedisBackend`/`PostgresBackend`/`QdrantBackend`.

## Tests de integración

Requieren Redis, Postgres y/o Qdrant reales:

```bash
docker compose -f docker-compose.test.yml up -d redis-test postgres-test qdrant-test
pytest tests/phoson_plugin_memory -q
```

Si el servicio correspondiente no está corriendo, esos tests de integración se saltan (no fallan); los tests unitarios de `MemoryPlugin`/`SemanticMemoryPlugin` corren siempre porque usan un backend fake.
