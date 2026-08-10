# Phoson Memory Plugin

Memoria de corto plazo (Redis, con TTL) para Phoson Agent, expuesta como tools nativas `memory_read` / `memory_write` (`AgentTool`, no `StructuredTool`).

Es la extracción de la forma de `core/memory/unified.py::UnifiedMemory` de Phoson-Core, desacoplada de Core. Los tiers de largo plazo (Postgres) y semántico (Qdrant) implementan la misma interfaz `MemoryBackend` y se agregan después sin tocar este plugin.

## Instalación

```bash
pip install "phoson-engine-minimal[memory]"  # instala redis
```

## Uso como Plugin

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

El modelo puede entonces llamar `memory_write(key, value, ttl_seconds=...)` y `memory_read(key)` como cualquier otra tool.

## Uso directo del backend

```python
from phoson_plugin_memory import RedisBackend

backend = RedisBackend(url="redis://localhost:6379/0", namespace="my-agent")
await backend.set("user_name", "Abel", ttl_seconds=3600)
value = await backend.get("user_name")
await backend.close()
```

## Extender con otro backend

Implementa `MemoryBackend` (`get`/`set`/`delete`/`list_keys`/`close`) y pásalo directamente a `MemoryPlugin` o úsalo standalone — no necesitas heredar de `RedisBackend`.

## Tests de integración

Requieren un Redis real:

```bash
docker compose -f docker-compose.test.yml up -d redis-test
pytest tests/phoson_plugin_memory -q
```

Si Redis no está corriendo, los tests de integración se saltan (no fallan); los tests unitarios de `MemoryPlugin` corren siempre porque usan un backend fake.
