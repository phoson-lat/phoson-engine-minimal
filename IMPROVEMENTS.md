# IMPROVEMENTS — phoson-engine-minimal / phoson-cli

> **Origen:** Roadmap activo de resolución de issues abiertos de GitHub para `phoson-engine-minimal` y `phoson-cli`.
>
> **Cómo usar este documento:** Cada ítem corresponde a un issue abierto en GitHub con su prioridad (P0–P2), estimación de esfuerzo (S/M/L), análisis de causa raíz, solución propuesta y criterios de aceptación.
>
> **Estado de referencia:** v0.15.0 · 1554 tests passing · pyright 0 errors (propio; 1 preexistente en `phoson_llm/chats/gemini.py` por tipado de librería) · ruff clean.

---

## Tabla resumen de Issues abiertos

| ID | Issue | Título | Prioridad | Esfuerzo | Impacto | Estado |
|----|-------|--------|-----------|----------|---------|--------|
| **I-91** | [#91](https://github.com/phoson-lat/phoson-engine-minimal/issues/91) | Context auto-compact gate subestima tokens & sin fallback en provider 400 | **P0** | M | 🔴 Crítico (bloquea sesiones largas) | ✅ Resuelto (v0.13.5) |
| **I-88** | [#88](https://github.com/phoson-lat/phoson-engine-minimal/issues/88) | Costo/uso en cabecera no se actualiza en vivo + costo OpenRouter USD en $0 | **P0** | S | 🔴 Alto (visibilidad de costos) | ✅ Resuelto (v0.13.6) |
| **I-89** | [#89](https://github.com/phoson-lat/phoson-engine-minimal/issues/89) | `/model` no persiste el provider junto con el modelo en `config.toml` | **P1** | S | 🟠 Medio (inconsistencia de config) | ✅ Resuelto (v0.13.7) |
| **I-82** | [#82](https://github.com/phoson-lat/phoson-engine-minimal/issues/82) | vLLM provider: HTTP 400 "No user query found in messages" con Qwen3.x | ~~P1~~ | — | — | ✅ Cerrado (no es bug nuestro — error de vLLM) |
| **I-83** | [#83](https://github.com/phoson-lat/phoson-engine-minimal/issues/83) | Compactar paneles de error a 1 línea y sobreescribir en cada reintento | **P1** | S-M | 🟠 Medio (ruido visual en TUI) | ✅ Resuelto (v0.13.8) |
| **I-84** | [#84](https://github.com/phoson-lat/phoson-engine-minimal/issues/84) | Reducción de uso de CPU en la TUI full-screen (idle y streaming) | **P1** | M | 🟠 Medio (eficiencia y batería) | ✅ Resuelto (v0.13.9) |
| **I-108** | [#108](https://github.com/phoson-lat/phoson-engine-minimal/issues/108) | Alt+Backspace se interpreta como doble-Esc: cancela el run en vuelo o abre el picker de rewind | **P1** | S-M | 🟠 Medio (fiabilidad de UX / cancelación accidental) | ✅ Resuelto (v0.15.0) |
| **I-109** | [#109](https://github.com/phoson-lat/phoson-engine-minimal/issues/109) | Rewind picker: lista viejo→nuevo e incluye entradas no-user (tool results como "(empty message)") | **P1** | S | 🟠 Medio (claridad de UX del rewind) | ✅ Resuelto (v0.15.0) |
| **I-113** | [#113](https://github.com/phoson-lat/phoson-engine-minimal/issues/113) | OpenRouter sin orden por `agentic_index` + `/model`/`/provider` requieren dos pasos y no marcan providers `unavailable` | **P2** | M | 🟡 Medio (UX de selección de modelo) | ✅ Resuelto (v0.13.10, hotfix v0.13.11) |
| **I-100** | [#100](https://github.com/phoson-lat/phoson-engine-minimal/issues/100) | Activar/desactivar MCPs a nivel servidor y nivel herramienta | **P2** | M-L | 🟡 Medio (gestión granular de tools) | ✅ Resuelto (v0.13.12) |
| **I-93** | [#93](https://github.com/phoson-lat/phoson-engine-minimal/issues/93) | Paquetes preconstruidos para Linux, macOS y Windows | **P2** | L | 🟢 Bajo (distribución binaria standalone) | ✅ Resuelto (v0.15.0) |

---

## Detalle de Issues y Plan de Acción

### I-91 — [Bug #91] Auto-compact subestima tokens & falta de fallback en error 400 de ventana de contexto
* **Estado:** ✅ **Resuelto (v0.13.5)** — gate conservador a nivel de request + rescate de emergencia ante 400 de contexto + compactación persistente (ver CHANGELOG v0.13.5).
* **Área:** `phoson_agent/plugins/summarizer.py`, `phoson_cli/controller.py`
* **Prioridad:** **P0** · **Esfuerzo:** M · **Impacto:** 🔴 Crítico
* **Problema:** 
  1. El gate de auto-compactación calcula tokens de forma optimista o incompleta (omitiendo overhead de tool definitions, reasoning o attachments), por lo que el auto-compact no dispara a tiempo.
  2. Cuando el proveedor rechaza la petición con HTTP 400 (context window exceeded / prompt too long), no hay un handler de recuperación que intente forzar la compactación de emergencia y reintentar.
* **Solución propuesta:**
  - Ajustar el cálculo/estimación del gate de token count para ser conservador e incluir el peso de schemas de herramientas y reasoning blocks.
  - Implementar interceptor en el controller / retry middleware: ante un error 400 identificable como "context length exceeded", disparar compactación de emergencia automática del historial y reintentar el turno una vez antes de fallar.
* **Criterio de listo:**
  - Test simulando límite de contexto: dispara auto-compact antes del 100%.
  - Test con mock de provider arrojando 400 context error: activa auto-compact de rescate y continúa la sesión.

---

### I-88 — [Bug #88] Actualización en vivo de tokens/costo en Header + captura de costo USD en OpenRouter
* **Estado:** ✅ **Resuelto (v0.13.6)** — costo `usage.cost` de OpenRouter autoritativo + métricas en vivo por step (ver CHANGELOG v0.13.6).
* **Área:** `phoson_cli/fullscreen/app.py`, `phoson_llm/chats/openrouter.py`
* **Prioridad:** **P0** · **Esfuerzo:** S · **Impacto:** 🔴 Alto
* **Problema:**
  1. El Header de la TUI no refresca el uso de tokens y costo en cada paso del run (solo salta al completarse el turno completo).
  2. El adapter de OpenRouter descarta los metadatos de costo en USD devueltos por la API o no los mapea a `TokenUsage` / métricas de sesión.
* **Solución propuesta:**
  - Enviar eventos de actualización al header durante `AgentStepDoneEvent` para reflejar consumo paso a paso.
  - Extraer el campo de costo en la respuesta streaming/final de OpenRouter y alimentar el acumulador de `SessionMetrics`.
* **Criterio de listo:**
  - Header actualiza sus números en vivo tras cada llamada a herramienta/step.
  - OpenRouter reporta costo mayor a `$0.0000` cuando la API entrega el costo del request.

---

### I-89 — [Enhancement #89] `/model` debe persistir el `provider` correspondiente en `config.toml`
* **Estado:** ✅ **Resuelto (v0.13.7)** — `/model` infiere el provider del modelo elegido (picker o prefijo `vendor/`, con excepción para routers) y persiste la dupla `(provider, model)`; rechaza guardar duplas sin credenciales (ver CHANGELOG v0.13.7).
* **Área:** `phoson_cli/commands.py`, `phoson_cli/config.py`, `phoson_cli/model_picker.py`
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio
* **Problema:** Al ejecutar `/model` (o seleccionarlo en el picker), solo se actualiza la clave `model` en `config.toml`, dejando la clave `provider` desalineada si el nuevo modelo pertenecía a otro proveedor.
* **Solución propuesta:**
  - Vincular la selección de modelo con su proveedor asociado (`provider, model`).
  - Al persistir `/model`, invocar `save_config` actualizando tanto `model` como `provider`.
* **Criterio de listo:**
  - Ejecutar `/model <modelo_de_otro_provider>` actualiza ambos campos en `~/.phoson/config.toml`.
  - Reiniciar el CLI mantiene la dupla `(provider, model)` correcta.

---

### I-82 — [Bug #82] vLLM: HTTP 400 "No user query found in messages" con modelos Qwen3.x
* **Estado:** ✅ **Cerrado (2026-08-28)** — Tras investigar, el 400 lo generaba el servidor vLLM (su procesamiento del chat template de Qwen), no la estructura de mensajes que envía phoson. No hay cambio requerido en el engine; issue cerrado en GitHub como no reproducible de nuestro lado.
* **Área:** `phoson_llm/chats/vllm.py`, `phoson_agent/agent.py`
* **Prioridad:** ~~P1~~ · **Esfuerzo:** — · **Impacto:** —
* **Problema (original):** El template de chat de Qwen en vLLM requería una estructura estricta de mensajes entre llamadas a herramientas (o rechazaba historiales donde no detecta un turno de usuario intercalado adecuadamente tras tool responses).
* **Resolución:**
  - Verificado que el payload enviado por el adapter de vLLM es estándar OpenAI tool-call compatible; el 400 provenía del lado del servidor vLLM.
  - Si reaparece en una versión concreta de vLLM, reabrir con: versión de vLLM + payload exacto + logs del servidor.

---

### I-83 — [Enhancement #83] Compactar errores del modelo a 1 línea y sobreescribir en cada reintento
* **Estado:** ✅ **Resuelto (v0.13.8)** — `render_error_notice()` de 1 línea compartido por ambos frontends; el sink fullscreen sobreescribe el notice pendiente en cada fallo y lo elimina cuando el reintento siguiente completa con éxito; el JSON crudo va a `logger.debug` (ver CHANGELOG v0.13.8).
* **Área:** `phoson_cli/formatting.py`, `phoson_cli/fullscreen/sink.py`, `phoson_cli/renderer.py`
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio
* **Problema:** Los errores de API/red renderizaban paneles grandes con JSON crudo que se apilaban con cada reintento, ensuciando el transcript.
* **Solución implementada:**
  - `render_error_notice(event, theme) -> Text`: `⚠ {code} · retryable — {hint}` (o mensaje saneado/truncado sin hint), sin JSON crudo.
  - `FullScreenSink._error_notice_idx`: reemplazo in-place en `AgentErrorEvent` repetido; `drop_error_notice()` en `AgentDoneEvent` (reintento exitoso) y en resets del transcript (`clear()`/rewind), con auto-reparación de índice obsoleto.
  - Classic `Renderer._on_error` imprime el notice (1 línea por reintento, no panel).
  - Nota de diseño: el issue proponía borrar el notice en `AgentStartEvent`; se hizo en `AgentDoneEvent` porque el engine siempre emite `start → error` por run (borrar en start haría de la sobreescripción código muerto) y el issue pide que desaparezca cuando el intento siguiente **exita**.
* **Criterio de listo:**
  - Tres reintentos fallidos ocupan solo 1 línea en el transcript en lugar de 3 paneles apilados. ✅

---

### I-84 — [Performance #84] Reducción de uso de CPU en TUI full-screen
* **Estado:** ✅ **Resuelto (v0.13.9)** — bug del throttle (`_touch()` incondicional en `on_event`), `min_redraw_interval=0.035`, spinner congelado solo durante streaming (cadencia 0.12 s intacta), header cacheado (ver CHANGELOG v0.13.9). Medido: streaming 29.6% → 4.1% CPU, thinking 8.3% → 4.3% CPU.
* **Plan de ataque:** ver `.opencode/plans/i84-cpu-idle-streaming.md`.
* **Área:** `phoson_cli/fullscreen/app.py`, `phoson_cli/fullscreen/sink.py`
* **Prioridad:** **P1** · **Esfuerzo:** M · **Impacto:** 🟠 Medio
* **Problema:** Uso continuo de 5–15% CPU en idle y 15–20% en streaming debido a re-renderizados innecesarios o tickers de animación muy agresivos.
* **Solución propuesta:**
  - Pausar los spinners / tickers cuando la aplicación esté en estado idle esperando input.
  - Throttling adaptativo del render durante el streaming de tokens.
* **Criterio de listo:**
  - Uso de CPU en idle cercano al 0% (<1%).
  - Reducción medible del consumo durante streaming sostenido.

---

### I-108 — [Bug #108] Alt+Backspace se interpreta como doble-Esc: cancela el run en vuelo o abre el picker de rewind
* **Estado:** ✅ **Resuelto (v0.15.0)** — guard de "Esc prefijo" vía `key_processor.input_queue`: un Esc solo cuenta (cancel/rewind) cuando la tecla siguiente en la cola NO es un payload Meta imprimible (ver abajo y CHANGELOG v0.15.0).
* **Área:** `phoson_cli/fullscreen/app.py`
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio
* **Problema:**
  1. El doble-Esc (rewind) se detecta **solo por tiempo** (`_REWIND_DOUBLE_ESC_WINDOW_SECONDS = 1.0` en `handle_escape()`), no por identidad de tecla.
  2. Muchos terminales codifican **Alt+<tecla>** como `ESC` + <tecla> (encoding Meta/Alt estándar). Para Alt+Backspace, prompt_toolkit expone ese `ESC` como un evento `escape` **indistinguible** de un Esc real.
  3. Consecuencias: con un run en vuelo, el prefijo `ESC` de Alt+Backspace **cancela el agente** (rama "in-flight → cancel"); en idle, arma/completa la ventana de doble-tap y **abre el picker de rewind** sin intención del usuario.
* **Solución propuesta:**
  - Distinguir un Esc **solo** (keypress limpio) de un `ESC` que es **prefijo** de una secuencia más larga o parte de un acorde con Alt: solo el primero debe contar para cancel/rewind.
  - Acortar la ventana de doble-tap (p. ej. ~300–500 ms) para reducir la probabilidad de capturar `ESC` ajenos (no resuelve por sí solo el caso de cancelación en vuelo).
  - Para la cancelación en vuelo: exigir un Esc limpio (sin otros bytes en una ventana corta) antes de cancelar.
  - Considerar desacoplar el single-Esc cancel y el doble-Esc rewind (hoy ambos montan sobre la acción `escape` y se remapan juntos).
* **Criterio de listo:**
  - Alt+Backspace (o cualquier tecla Alt-modificada con prefijo `ESC`) **no** cancela un run en vuelo ni abre el picker en idle.
  - Doble-Esc deliberado sigue abriendo el picker; single-Esc en vuelo sigue cancelando de inmediato (regresión #68 intacta).
  - Tests de enrutado de teclas vía PipeInput con bytes `ESC`+<key> que no disparan cancel/rewind.

---

### I-109 — [Bug #109] Rewind picker: orden viejo→nuevo e inclusión de entradas no-user (tool results)
* **Estado:** ✅ **Resuelto (v0.15.0)** — `jump_candidates()` recorre el path en reversa (nuevo→viejo) y hace el filtro consciente del contenido: un nodo role-`user` solo califica si su contenido es `str` o contiene al menos un `TextBlock` (ver CHANGELOG v0.15.0).
* **Área:** `phoson_cli/controller.py`, `phoson_cli/rewind_picker.py`
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio
* **Problema:**
  1. **Orden:** `SessionController.jump_candidates()` recorre el path activo en orden `root → cursor` y añade los candidatos a medida que avanza, por lo que el picker lista **viejo → nuevo** (índice 1 = turno más antiguo) con el cursor inicial en el más viejo. Debería ser **nuevo → viejo**, con el cursor en el mensaje más reciente (el más probable objetivo de un rewind).
  2. **Contenido:** el filtro es **por role** (`message.role != "user"`), pero el engine guarda los **tool results con role `user`** (`phoson_agent/_tool_runner.py` añade `Message(role="user", content=[ToolResultBlock(...)])`; ver docstring de `ToolResultBlock` en `phoson_llm/schemas/inputs.py`). Esos nodos pasan el filtro y, al no contener `TextBlock`, se renderizan como filas **"(empty message)"** en el picker. (Assistant/system ya están excluidos por el role check.)
* **Solución propuesta:**
  - **Orden:** invertir la lista de candidatos (o construirla en reversa) para que sea nuevo→viejo y el cursor inicial quede en el turno de usuario más reciente.
  - **Contenido:** hacer el filtro **consciente del contenido**, no solo del role: incluir un nodo solo si es un turno de usuario genuino — contenido `str` o con al menos un `TextBlock` — y excluir nodos cuyo contenido sea solo `ToolResultBlock` (tool results), manteniendo la exclusión de roles no-`user`.
* **Criterio de listo:**
  - El picker lista **solo** mensajes del usuario, ordenados **nuevo → viejo**, con el cursor inicial en el más reciente.
  - Conversación con tool calls: el picker no muestra filas "(empty message)" ni nodos de tool results.
  - Tests: `jump_candidates` con un path que mezcla user/assistant/tool-result(user-role) devuelve solo los turnos de usuario genuinos, en orden nuevo→viejo.

---

### I-113 — [Enhancement #113] OpenRouter: orden por `agentic_index` + unificar `/model`/`/provider` en un solo picker con marcado `unavailable`
* **Estado:** ✅ **Resuelto (v0.13.10, hotfix v0.13.11)** — [PR #116](https://github.com/phoson-lat/phoson-engine-minimal/pull/116). OpenRouter ordenado por `agentic_index` desc (sin campo → al final, alfabético; current siempre primero); `list_models_for_providers()` concurrente; picker unificado multi-provider en ambos frontends (selección cambia `(model, provider)` juntos, reusando I-89); providers con fetch fallido marcados `unavailable` en picker y `/model list` (internamente `ModelListingError`; el fast path de 1 provider conserva fallback+warning exactos); docs sin la caché inexistente. Incluye tres fixes de corrección post-review encontrados validando en vivo: (1) `/model <id>` explícito ahora siempre resuelve el provider real vía listing (antes solo lo hacía la rama del picker); (2) se corrigió la ambigüedad del heurístico de prefijo cuando un router (OpenRouter) re-expone el catálogo de otro vendor tal cual; (3) **hotfix v0.13.11** ([PR #117](https://github.com/phoson-lat/phoson-engine-minimal/pull/117)): el lookup de provider estaba gateado en `"/" in id`, así que los ids *sin* prefijo de servidores locales (vLLM/Ollama/LM Studio, p. ej. `Qwen3.8-27B-FP8`) nunca cambiaban el provider — ahora el lookup siempre corre y prefiere un provider distinto al activo cuando varios lo listan (ver CHANGELOG v0.13.10/v0.13.11 y plan `.opencode/plans/i113-model-picker-unified.md`).
* **Área:** `phoson_cli/model_selector.py`, `phoson_cli/model_picker.py`, `phoson_cli/commands.py`, `phoson_cli/command_host.py`, `phoson_cli/fullscreen/command_host.py`, `phoson_cli/fullscreen/model_cache.py`, `phoson_cli/models.py`, `docs/api/phoson_cli.md`, `README.md`
* **Prioridad:** **P2** · **Esfuerzo:** M · **Impacto:** 🟡 Medio
* **Problema:**
  1. **Sin orden útil en OpenRouter.** `_prioritize_current()` ordena todos los proveedores (incluido OpenRouter) por `id.lower()` alfabético, con solo el modelo actual fijado primero. La API de OpenRouter ya devuelve `benchmarks.artificial_analysis.agentic_index` para una parte importante del catálogo (~205 de 381 modelos a la fecha), señal mucho más relevante para elegir un modelo orientado a agentes/tool-use que el orden alfabético — y hoy no se usa.
  2. **Selección en dos pasos.** El flujo actual obliga a `/provider` (picker propio) y luego `/model` (que solo lista el proveedor *activo*); no existe una vista única que muestre modelos de todos los proveedores configurados a la vez, así que comparar "qué modelo, de qué proveedor" implica saltar entre dos pickers.
  3. **Fallo de listado silencioso e indistinguible.** Cuando el listado en vivo de un proveedor falla (timeout, 401, rate limit…), cada `_list_<provider>_models()` emite un `UserWarning` y degrada a una lista de 1 modelo (el modelo actual) — indistinguible de un proveedor que legítimamente solo tiene un modelo. No hay marcador visible de `unavailable` ni en el picker ni en `/model list`.
  4. **Nota aclaratoria (no es un problema nuevo):** la caché en disco de `~/.phoson/models.json` para el listado de modelos **ya no existe** — `list_available_models()` siempre hace fetch en vivo y no lee/escribe la sección `cache` (cubierto por `test_list_available_models_never_writes_models_json` / `test_list_available_models_always_calls_the_live_fetcher`). Lo único desactualizado es que `docs/api/phoson_cli.md` (sección "Model registry") todavía describe esa caché como si existiera ("instant picker, TTL 24h, works offline") — corregir esa documentación es parte de este issue.
* **Solución propuesta:**
  - En `_list_openrouter_models`, ordenar por `benchmarks.artificial_analysis.agentic_index` descendente antes de pasar por `_prioritize_current` (que debe seguir fijando el modelo actual primero); los modelos sin el campo van al final, entre ellos en el orden alfabético actual. Considerar que `_prioritize_current` acepte un comparador/clave secundaria en vez de tener `id.lower()` hardcodeado, ya que el criterio es específico de OpenRouter.
  - Añadir algo como `list_available_models_for_providers(config, providers) -> list[ProviderListing]` que consulte todos los proveedores configurados (`enabled_providers_from_config()`) **concurrentemente** (`asyncio.gather(..., return_exceptions=True)` o equivalente), devolviendo por proveedor su lista de `ModelOption` o un error.
  - Nuevo picker unificado (o modo nuevo de `model_picker.py`) que muestre todos los modelos de todos los proveedores configurados en una sola lista, cada fila con `id  (provider)`, y una sección/marca separada para los proveedores que fallaron (`unavailable`). Seleccionar una fila de otro proveedor debe cambiar `(model, provider)` juntos, reusando la persistencia de I-89 (`set_model(model, provider=...)`).
  - Colapsar/ajustar `pick_model`/`pick_provider` en `command_host.py` (clásico) y `fullscreen/command_host.py` (que hoy ni abre Float para modelo, solo autocompletado inline) para que ambos frontends expongan el nuevo picker unificado sin romper el modo autocompletado inline del full-screen.
  - Actualizar `docs/api/phoson_cli.md` quitando el ejemplo de `cache` y la afirmación de "instant, works offline (TTL 24h)".
* **Criterio de listo:**
  - OpenRouter en `/model`, autocompletado inline y `/model list` ordenado por `agentic_index` descendente, modelo actual siempre primero; sin el campo van al final.
  - Un solo picker/lista muestra modelos de todos los proveedores configurados, cada entrada con su proveedor entre paréntesis.
  - Elegir un modelo de otro proveedor cambia ambos (modelo + proveedor) sin pasar por `/provider`.
  - Un proveedor cuyo listado en vivo falla se marca visiblemente `unavailable` (no un fallback silencioso de 1 modelo) en el picker y en `/model list`.
  - El fetch de listados de múltiples proveedores es concurrente, no secuencial.
  - `docs/api/phoson_cli.md` ya no describe una caché de listado de modelos persistida en disco.
  - Tests nuevos/actualizados: orden por `agentic_index` (con y sin el campo), agregación multi-proveedor, marcado de proveedor `unavailable`; `ruff format`/`ruff check`/`pyright`/`pytest` limpios.

---

### I-100 — [Feature #100] Habilitar / Deshabilitar MCPs a nivel servidor y herramienta
* **Estado:** ✅ **Resuelto (v0.13.12)** — flags `enabled`/`tools` en `mcps.json` (retrocompatibles), `/mcp toggle <server> [tool]` con reapply en vivo, guards de ejecución `ServerDisabled`/`ToolDisabled`, marcado `(disabled)` en `/mcp status` (ver CHANGELOG v0.13.12).
* **Plan de ataque:** ver `.opencode/plans/i100-mcp-server-tool-toggle.md`.
* **Área:** `phoson_plugin_mcp/_plugin.py`, `phoson_cli/_mcp_commands.py`, `phoson_cli/session_utils.py`, `docs/mcp-cli.md`, `mcps.json.example`
* **Prioridad:** **P2** · **Esfuerzo:** M-L · **Impacto:** 🟡 Medio
* **Problema:** No existe forma de desactivar temporalmente un servidor MCP completo o una herramienta MCP específica sin borrar la configuración.
* **Solución propuesta:**
  - Añadir soporte para flag `enabled: bool` en la configuración de MCP servers y en tools individuales.
  - Comando `/mcp toggle <server> [tool]` y filtrado en el registro de tools expuestas al modelo.
* **Criterio de listo:**
  - Servidor desactivado no expone ninguna de sus herramientas.
  - Herramienta específica desactivada se omite del schema enviado al LLM.

---

### I-93 — [Feature #93] Empaquetado binario preconstruido (Linux / macOS / Windows)
* **Estado:** ✅ **Resuelto (v0.15.0)** — spec de PyInstaller (`phoson_cli.spec`) + workflow `release-binaries.yml` (5 plataformas: linux x86_64/arm64, darwin x86_64/arm64, windows x86_64) que adjunta los binarios a cada release; helpers de runtime congelado en `phoson_cli/_frozen.py` (resolución de assets vía `sys._MEIPASS`, versión inyectada en build) y modo `FROZEN` en el updater.
* **Área:** `.github/workflows/release-binaries.yml`, `phoson_cli.spec`, `phoson_cli/_frozen.py`, `phoson_cli/_views.py`, `phoson_cli/installer.py`, `phoson_cli/updater.py`
* **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟢 Bajo
* **Problema:** Requiere entorno Python ≥3.12 y herramientas de gestión (`uv`/`pip`) para la instalación de usuarios finales.
* **Solución implementada:**
  - `phoson_cli.spec`: entry point `phoson_cli/__main__.py`; data asset `phos-ascii.txt` staged bajo `phoson_cli/`; hidden imports para los SDK de providers/plugins que se importan con lazy-import (gemini, mistral, boto3, mcp, asyncpg, redis, qdrant) + `collect_submodules` de los 6 paquetes propios (el plugin loader los importa dinámicamente); flag `--version X.Y.Z` inyecta `phoson_cli/_frozen_version.txt` en el bundle.
  - `.github/workflows/release-binaries.yml`: matrix de 5 runners (ubuntu-latest, ubuntu-24.04-arm, macos-latest=arm64, macos-13=x86_64, windows-latest); versión tomada del tag del release (`v0.15.0` → `0.15.0`); build con `uv sync --no-install-project --all-extras` + `pyinstaller phoson_cli.spec --version=<v>`; job `attach` que renombra los artifacts a los nombres de la tabla del README y los sube con `softprops/action-gh-release`.
  - `phoson_cli/_frozen.py`: `asset_path()` resuelve assets en `sys._MEIPASS/phoson_cli/` (bundle onefile) o junto al módulo (source); `is_frozen()` desde `sys.frozen`; `frozen_version()` lee `_frozen_version.txt` inyectado en build (el bundle no trae metadata de paquete).
  - `updater.py`: nuevo `InstallMode.FROZEN` (detectado primero, antes que uv/pip); `get_current_version()` usa `frozen_version` cuando está congelado; `manual_hint` para frozen apunta a la página de Releases.
  - README: sección "Standalone binaries (no Python required)" con la tabla de assets.
* **Criterio de listo:**
  - Binarios autónomos descargables desde la sección de Releases de GitHub. ✅ (workflow publicado; se verifica en el primer release)
  - `phoson-cli --version` funciona dentro del binario (versión inyectada, sin metadata).
  - `test_frozen_unit.py`: 12 tests de asset_path (source + MEIPASS), is_frozen, frozen_version, integración updater (modo FROZEN).

---

## Roadmap sugerido de ataque

```
Sprint Próximo (Estabilidad de Contexto & Métricas)
├── I-91 (Auto-compact gate + fallback 400) ✅ v0.13.5
├── I-88 (Header live metrics + OpenRouter USD cost) ✅ v0.13.6
└── I-89 (/model persiste provider en config.toml) ✅ v0.13.7

Sprint Siguiente (UX & Performance)
├── I-108 (Alt+Backspace no debe leerse como doble-Esc / cancel) ✅ v0.15.0
├── I-109 (Rewind picker: orden nuevo→viejo y solo mensajes user) ✅ v0.15.0
├── I-83 (Compactar paneles de error a 1 línea en reintentos) ✅ v0.13.8
├── I-84 (Optimización de CPU en idle/streaming) ✅ v0.13.9
└── I-82 (vLLM Qwen3.x) ✅ Cerrado — error de vLLM, no del engine

Sprint Posterior (Ecosistema & Distribución)
├── I-113 (OpenRouter agentic_index sort + picker unificado /model+/provider) ✅ v0.13.10 + hotfix v0.13.11
├── I-100 (Toggle granular MCP servers & tools) ✅ v0.13.12
└── I-93 (Binarios precompilados standalone en CI) ✅ v0.15.0
```

## Principios de desarrollo

1. **Mantener paridad entre frontends:** Cualquier render nuevo debe ser una función pura en `formatting.py` utilizable en modo fullscreen y clásico.
2. **Cobertura de tests rigurosa:** Cada corrección o feature debe incluir tests unitarios/e2e y pasar validación estricta de `ruff` y `pyright`.
3. **Optimización con métricas:** Todo cambio de performance (CPU, tokens, tiempo) debe incluir benchmark o medición verificable.
