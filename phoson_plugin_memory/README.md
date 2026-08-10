# Phoson Memory Plugin

Memoria para Phoson Agent, expuesta como tools nativas `memory_read` / `memory_write` (`AgentTool`, no `StructuredTool`), con dos tiers intercambiables detrás de la misma interfaz `MemoryBackend`:

- **Redis** (corto plazo, con TTL) — default.
- **Postgres** (largo plazo) — mismo TTL, pero enforced a nivel de lectura (ver abajo).

Es la extracción de la forma de `core/memory/unified.py::UnifiedMemory` de Phoson-Core, desacoplada de Core. Un tier semántico (Qdrant) necesitaría una interfaz distinta (`search(query, top_k)` por similitud, no lookup por key exacta) y queda pendiente hasta que haya un caso de uso concreto.

## Instalación

```bash
pip install "phoson-engine-minimal[memory]"  # instala redis + asyncpg
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

El modelo puede entonces llamar `memory_write(key, value, ttl_seconds=...)` y `memory_read(key)` como cualquier otra tool — el comportamiento es idéntico sin importar el backend elegido.

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

## TTL en Postgres

A diferencia de Redis, Postgres no expira keys automáticamente. `PostgresBackend` filtra por `expires_at` en cada lectura (`get`/`list_keys`), así que un valor expirado nunca se devuelve — pero la fila queda en la tabla hasta que alguien la borre. Llama `await backend.purge_expired()` periódicamente (cron, tarea de background) si escribes muchas entradas con TTL corto, para no acumular basura sin límite.

## Extender con otro backend

Implementa `MemoryBackend` (`get`/`set`/`delete`/`list_keys`/`close`) y pásalo directamente a `MemoryPlugin` o úsalo standalone — no necesitas heredar de `RedisBackend`/`PostgresBackend`.

## Tests de integración

Requieren Redis y/o Postgres reales:

```bash
docker compose -f docker-compose.test.yml up -d redis-test postgres-test
pytest tests/phoson_plugin_memory -q
```

Si el servicio correspondiente no está corriendo, esos tests de integración se saltan (no fallan); los tests unitarios de `MemoryPlugin` corren siempre porque usan un backend fake.
