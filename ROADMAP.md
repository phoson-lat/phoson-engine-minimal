# ROADMAP — phoson-engine-minimal

> Semana del 10 al 16 de agosto de 2026.
> Contexto completo: `Phoson-Core` va a dejar de usar LangChain/LangGraph y correr sobre este engine. Ver [`Phoson-Core/ROADMAP.md`](../Phoson-Core/ROADMAP.md) para el lado espejo de esta migración.

---

## Por qué esto importa ahora

Phoson-Core necesita de este repo dos piezas que hoy no existen: **persistencia de sesión** (hoy usa `AsyncPostgresSaver` de LangGraph) y **memoria tiered** (hoy usa su propio `UnifiedMemory`, ya construido pero acoplado a Core). Ambas están en el roadmap OSS original como `phoson-plugin-checkpoint` (P1) y `phoson-plugin-memory` (P0), pero sin una línea de código todavía.

Construirlas aquí no es solo requisito de migración — son exactamente las "Advanced Plugins" que `Business-Model.md` define como línea de ingreso propia (`$X/agente/mes`). Un solo esfuerzo cubre roadmap OSS + habilita migración + genera el producto pagado.

---

## Esta semana

- [x] **Decidir la interfaz canónica de plugin.**
  Hoy coexisten dos contratos: `Plugin` real en `phoson_agent/plugin.py` (sync: `configure/initialize/cleanup`) y `PhosonPlugin` async (`on_load/on_unload`) descrito en el roadmap externo pero nunca implementado. Elegir uno, documentarlo en `docs/plugins.md`, y dejar registrada la decisión (y el porqué) en este archivo.
  **Criterio de listo:** `docs/plugins.md` refleja una sola interfaz sin ambigüedad.
  **Decisión:** se mantiene `Plugin` (sync) como único contrato. `PhosonPlugin` async nunca tuvo implementación, loader, ni un solo plugin real que lo usara — `PluginRegistry`, `phoson_plugin_mcp` y todos los ejemplos ya asumen `Plugin`. La parte async que de verdad importa (pools de conexión, I/O) vive dentro de `initialize()`/`get_tools()`/tool handlers async, no en el lifecycle del propio plugin. Ver `docs/plugins.md#decisión-de-interfaz-canónica`. `phoson_plugin_checkpoint` y `phoson_plugin_memory` (ver abajo) son la prueba de que el contrato sync alcanza incluso para backends 100% async (Postgres, Redis).

- [x] **Scaffold de `phoson_plugin_checkpoint`.**
  Implementación de `SessionStorage` (la ABC ya existe en `phoson_agent/sessions/`) respaldada en Postgres, con su propio esquema (no depender de tablas de Core). Debe soportar `save`/`load`/`list_sessions`/`delete` de forma async real (no `asyncio.to_thread` como `JsonlStorage`).
  **Criterio de listo:** un test de integración que guarda y recupera un `ConversationTree` completo contra un Postgres real (docker-compose de test).
  **Hecho:** `phoson_plugin_checkpoint/storage.py::PostgresStorage`, esquema propio (`phoson_checkpoint_sessions`/`phoson_checkpoint_nodes`), asyncpg puro (sin `to_thread`). 10 tests de integración en `tests/phoson_plugin_checkpoint/` contra Postgres real vía `docker-compose.test.yml` (se saltan, no fallan, si Postgres no está corriendo o `asyncpg` no está instalado).

- [x] **Scaffold de `phoson_plugin_memory` — solo tier corto plazo (Redis).**
  Extraer la forma (no necesariamente el código línea por línea) de `core/memory/unified.py::UnifiedMemory` de Phoson-Core: interfaz `MemoryBackend` + implementación Redis con TTL, expuesta como `get_tools()` (`memory_read`/`memory_write` como `AgentTool`, no `StructuredTool`).
  Postgres (long-term) y Qdrant (semantic) quedan para la próxima semana — no bloquear el scaffold por cubrir los 3 tiers de una vez.
  **Criterio de listo:** un agente de ejemplo en `examples/` usando memoria Redis end-to-end (reemplaza al ejemplo educativo actual en memoria de proceso).
  **Hecho:** `phoson_plugin_memory/backend.py::MemoryBackend` + `redis_backend.py::RedisBackend`. `examples/plugin_example_memory.py` reescrito: dos `AgentEngine` separados donde el segundo lee lo que escribió el primero via Redis. Tests unitarios (backend fake) + integración (Redis real, mismo patrón skip-si-no-hay-servicio que checkpoint).

- [x] **Tier Postgres de `phoson_plugin_memory`** (adelantado desde "Bloqueado" — el scaffold Redis ya quedó estable).
  Mismo `MemoryBackend`, ahora seleccionable vía `config: {"backend": "postgres", "dsn": "..."}` en `MemoryPlugin` (default sigue siendo `"redis"`). Esquema propio (`phoson_memory_entries`, namespaced por `namespace`), sin tocar tablas de `phoson_plugin_checkpoint` aunque compartan el mismo Postgres de test.
  **Hecho:** `phoson_plugin_memory/postgres_backend.py::PostgresBackend`. Postgres no expira keys solo — TTL se enforce filtrando `expires_at` en cada lectura, con `purge_expired()` para limpiar filas vencidas (documentado como tarea periódica, no automática). 13 tests de integración nuevos en `tests/phoson_plugin_memory/test_postgres_backend.py` (incluye TTL real con `asyncio.sleep`, aislamiento por namespace, y `purge_expired`) + tests de selección de backend en `MemoryPlugin`.

- [x] **Tier Qdrant (semántico) de `phoson_plugin_memory`** (adelantado desde "Bloqueado").
  A diferencia de Postgres, no es el mismo `MemoryBackend` con otro storage: búsqueda semántica es "dame lo más parecido a X", no "dame el valor de la key X" — necesita una interfaz propia y una decisión de embedder (verificado con el usuario antes de tocar código: sin infraestructura de embeddings previa en el repo).
  **Decisión de embedder:** `embed_fn` inyectable (`str -> list[float]`, sync o async), sin proveedor por default — evita meter una dependencia pesada (sentence-transformers) o atar el plugin a una API key de un proveedor específico. Mantiene el engine "minimal".
  **Hecho:** `phoson_plugin_memory/semantic_backend.py::SemanticMemoryBackend` (interfaz nueva: `upsert`/`search`/`delete`/`close`, no extiende `MemoryBackend`) + `qdrant_backend.py::QdrantBackend`. Plugin separado, `semantic_plugin.py::SemanticMemoryPlugin`, expone `memory_remember`/`memory_recall` (no `memory_read`/`memory_write` — el contrato de tool es distinto). Validado contra un Qdrant real antes de escribir el código final: los IDs de punto deben ser UUID/entero, no string arbitrario (se deriva un UUID5 de `namespace:key`); coleccion se crea sola en el primer `upsert`, tamaño de vector inferido del embedding. 20 tests contra Qdrant real en `tests/phoson_plugin_memory/test_qdrant_backend.py` (ranking por similitud con un embedding de prueba determinista, sin modelo pesado) + `test_semantic_plugin.py`.

- [x] **Cerrar gaps de `phoson_plugin_memory` detectados al revisar los 3 tiers juntos.**
  Al confirmar que los 3 tiers estaban completos, salieron tres huecos concretos (no cosméticos):
  1. **Colisión de nombres de tool entre instancias de `MemoryPlugin`** — combinar Redis + Postgres en el mismo agente hacía que la segunda instancia pisara silenciosamente las tools de la primera (`AgentEngine._tools_by_name` es un dict, sobreescribe sin avisar). Se agregó `tool_prefix` a `MemoryPlugin` y `SemanticMemoryPlugin`.
  2. **No había tools de borrado/listado.** `delete()`/`list_keys()` existían en los backends pero no eran invocables por el modelo. Se agregaron `memory_delete`/`memory_list` (`MemoryPlugin`) y `memory_forget` (`SemanticMemoryPlugin`).
  3. **`purge_expired()` de Postgres era 100% manual.** Se agregó `purge_interval_seconds` en `MemoryPlugin`: arranca un background task lazy (en la primera llamada a una tool, porque `initialize()` no garantiza un loop corriendo) que se cancela en `cleanup()`/`aclose()`. Probado contra Postgres real, no solo con un backend fake.
  **Explícitamente fuera de este cierre** (decisiones ya tomadas, no re-abrirlas sin conversarlo): un embedder por default para `SemanticMemoryPlugin` (se decidió `embed_fn` inyectable, ver arriba) y una capa de orquestación tipo `UnifiedMemory` que decida sola dónde leer/escribir entre tiers (no hay spec de esa política — combinar tiers hoy es explícito, agregando cada plugin por separado).

- [x] **Arreglar pooling de sesión en `phoson_plugin_mcp`.**
  Hoy cada tool call reconecta y reinicializa la sesión MCP (`_execute_stdio/_execute_sse/_execute_http` en `plugin.py`), incluso lanzando un subprocess nuevo para stdio. Cachear la sesión/conexión, no solo las definiciones de tools.
  **Criterio de listo:** benchmark simple mostrando reducción de latencia en llamadas sucesivas a la misma tool MCP.
  **Hecho:** `_get_session`/`_call_tool_on_cached_session` reemplazan `_execute_stdio/_execute_sse/_execute_http` — una sesión por servidor, cacheada en un `AsyncExitStack`, con auto-reconexión si la sesión cacheada falla. `scripts/benchmark_mcp_pooling.py` mide contra un servidor STDIO local real (`tests/phoson_plugin_mcp/fixtures/echo_server.py`, sin dependencia de red): **~11x** menos latencia en 10 llamadas sucesivas (991ms/call sin pooling → 91ms/call con pooling). 5 tests nuevos en `tests/phoson_plugin_mcp/test_session_pooling.py` contra el mismo servidor real. De paso se corrigió un bug de colisión de nombres (`tests/phoson_plugin_mcp/__init__.py` sombreaba el paquete real `phoson_plugin_mcp`, dejando sus 13 tests siempre en skip).

## Bloqueado / después de esta semana

- `phoson_http` (modo daemon) — no es necesario para la migración de Core (que va a embeber el engine como librería, no como servicio separado). Queda pausado hasta que haya un caso de uso real que lo justifique.
- Actualizar `CHANGELOG.md` (hoy solo documenta hasta v0.2.2 y el repo ya va en v0.2.4) — hacerlo junto con el release que incluya los plugins nuevos, no antes.

## Ver también

- [`Phoson-Core/ROADMAP.md`](../Phoson-Core/ROADMAP.md) — plan de migración del lado consumidor.
- `TODO.md` — deuda técnica de calidad (no bloquea lo de arriba, pero conviene no perderla de vista: contrato de `ToolHandler`, mutación global de `sys.path` en el plugin loader).
