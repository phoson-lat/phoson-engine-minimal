# IMPROVEMENTS — phoson-engine-minimal / phoson-cli

> **Origen:** Roadmap activo de resolución de issues abiertos de GitHub para `phoson-engine-minimal` y `phoson-cli`.
>
> **Cómo usar este documento:** Cada ítem corresponde a un issue abierto en GitHub con su prioridad (P0–P2), estimación de esfuerzo (S/M/L), análisis de causa raíz, solución propuesta y criterios de aceptación.
>
> **Estado de referencia:** v0.16.1 · 1600 tests passing · pyright 0 errors (propio; 1 preexistente en `phoson_llm/chats/gemini.py` por tipado de librería) · ruff clean.

---

## Tabla resumen de Issues abiertos

| ID | Issue | Título | Prioridad | Esfuerzo | Impacto | Estado |
|----|-------|--------|-----------|----------|---------|--------|
| **I-128** | [#128](https://github.com/phoson-lat/phoson-engine-minimal/issues/128) | Sin feedback en UI mientras el modelo compone la tool call (brecha silenciosa antes de la línea de tool-start) | **P1** | S-M | 🟠 Medio (percepción de congelamiento en tools largas) | ✅ Resuelto (v0.16.0) |
| **I-119** | [#119](https://github.com/phoson-lat/phoson-engine-minimal/issues/119) | Cargar una conversación con attachments temporales borrados crash: `FileNotFoundError` en `file:///tmp/...` | **P1** | S | 🟠 Medio (bloquea reabrir sesiones) | ✅ Resuelto (v0.16.1) |
| **I-127** | [#127](https://github.com/phoson-lat/phoson-engine-minimal/issues/127) | Bash tool: timeout hardcodeado a 30s que el agente no puede subir ni bajar | **P1** | S | 🟠 Medio (mata builds/tests largos) | ✅ Resuelto (v0.17.0, ext. a sub-agents) |
| **I-112** | [#112](https://github.com/phoson-lat/phoson-engine-minimal/issues/112) | Python `UserWarning` impreso a stderr además del warning estilizado del CLI | **P2** | S | 🟡 Medio (ruido visual, expone paths) | ✅ Resuelto (v0.17.1) |
| **I-110** | [#110](https://github.com/phoson-lat/phoson-engine-minimal/issues/110) | Plugin system: extender look & commands del CLI, no solo el engine | **P2** | L | 🟡 Medio (extensibilidad/ecosistema) | ✅ Resuelto (v0.18.0) |
| **I-126** | [#126](https://github.com/phoson-lat/phoson-engine-minimal/issues/126) | Nuevo plugin oficial: monitores de larga duración que reactivan al agente | **P2** | L | 🟢 Bajo (feature de roadmap) | ✅ Resuelto (v0.19.0) |
| **I-115** | [#115](https://github.com/phoson-lat/phoson-engine-minimal/issues/115) | docs: refrescar README — contenido obsoleto, comprimir sección CLI, assets visuales | **P2** | M | 🟢 Bajo (calidad de docs) | ✅ Resuelto (PR) |
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

## Detalle de Issues Abiertos y Plan de Acción

### I-128 — [Feature #128] Sin feedback en UI mientras el modelo compone la tool call
* **Estado:** ✅ **Resuelto (v0.16.0)**
* **Área:** `phoson_agent/_loop.py`, `phoson_agent/models.py`, `phoson_cli/fullscreen/sink.py`, `phoson_cli/fullscreen/render.py`, `phoson_cli/renderer.py`, `phoson_cli/formatting.py`, `docs/api/phoson_agent.md`
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio (la UI parece congelada en tool calls con args largos)
* **Resolución (resumen):** nuevo `AgentToolComposingEvent` (throttle leading-edge ~250 ms) emitido desde `_consume_llm_stream()`; fullscreen lo pinta en la activity line del pane (`⚙ writing file…`) siguiendo al texto ya streamado, con header `Composing tool`; clásico relabelfea el spinner a `⚙ {verb}…`. El glyph se anima en las fases thinking/composing/running-tool (la card start es estática hasta el done). Ver `docs/plans/I-128.md`.
* **Problema:**
  1. Desde que el modelo empieza a emitir los deltas de una tool call hasta que la tool **empieza a ejecutarse**, no hay nada nuevo en la UI: el pane fullscreen se queda quieto (el header solo dice `thinking · step N/M` o `Streaming` residual, porque `running_tool` solo se marca con `AgentToolStartEvent`) y el clásico mantiene el último label del spinner. La línea de verbo (`⚙ writing file · <path>`) solo aparece con la tool call **completa** acumulada.
  2. En `write_file` con 200 líneas o un `bash` multi-línea, el usuario espera la generación completa de los args sin saber qué va a pasar (el verbo se desconoce hasta que el nombre termina de streamear, el detalle hasta que los args terminan).
* **Causa raíz (verificada en código):**
  1. `_openai_compatible.py` (~línea 485) **sí emite** `ToolCallDeltaEvent(index, tool_name, args_chunk)` por chunk, y `ToolCallEvent` completo en `finish_reason == "tool_calls"`.
  2. `phoson_agent/_loop.py::_consume_llm_stream()` (~línea 160) **descarta los deltas**: solo maneja `TokenEvent`, `ReasoningTokenEvent`, `ToolCallEvent`, `UsageEvent`, `LLMDoneEvent`, `ErrorEvent`. No existe ninguna señal de "se está componiendo una tool call" en el stream de eventos del agente.
  3. `AgentToolStartEvent` solo lo emite `_tool_runner.py` en tiempo de ejecución (tras permisos/`_apply_before_tool`). La infra de verbos ya existe (`_TOOL_VERBS` en `formatting.py`: `write_file → "writing file"`, `bash → "running command"`…) pero solo está cableada a start/done.
  4. No existe un evento de agente para la fase de composición (`models.py`: Start/Token/Reasoning/ToolStart/ToolDone/StepDone/Done/Error).
* **Solución propuesta:**
  - Manejar `ToolCallDeltaEvent` en `_consume_llm_stream()` y emitir un nuevo `AgentToolComposingEvent(index, tool_call_id, tool_name, args_chunk)`: al menos en el primer delta no vacío (cuando el nombre ya se conoce) y, opcionalmente, con throttle leading-edge (~250 ms / 10 chunks) para mantener el estado vivo sin inundar.
  - Fullscreen: línea de composición en el pane (`⚙ writing file…` / `⚙ composing tool call…`) que se **reemplaza in-place** por la card de `AgentToolStartEvent` (misma mecánica de identidad que ya usa start→done); si el stream falla antes del start, la línea la limpia el path de errores (I-83).
  - Clásico: relabel del spinner a `⚙ {verb}…` en `_on_tool_composing()`.
  - Header: `status_text()` gana el estado `Composing tool` (flag `turn.composing_tool`).
  - Docs: nuevo evento documentado en `docs/api/phoson_agent.md`.
* **Criterio de listo:**
  - Test: stream con `ToolCallDeltaEvent`s produce `AgentToolComposingEvent` antes de cualquier `AgentToolStartEvent` (con nombre en cuanto se conoce); streams de solo texto/reasoning no emiten composing.
  - Fullscreen: línea de composición visible durante la generación, reemplazada in-place (sin duplicados ni líneas huérfanas).
  - Clásico: el label del spinner cambia al verbo de composición.
  - `ruff format`/`ruff check`/`pyright`/`pytest` limpios.

---

### I-119 — [Bug #119] Cargar una conversación con attachments temporales borrados crash
* **Estado:** ✅ **Resuelto (v0.16.1)**
* **Área:** `phoson_llm/utils.py`, `phoson_llm/chats/_openai_compatible.py`, `phoson_llm/chats/anthropic.py`, `phoson_llm/chats/gemini.py`, `tests/phoson_llm/test_missing_attachment_unit.py`
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio (un crash que impide reabrir la sesión)
* **Resolución (resumen):** `load_file_as_base64()` ahora retorna `str | None` (archivo ausente/ilegible → `logger.warning` + `None`, catch de `OSError`); los **6** call sites `file://` (OpenAI-compat: image+audio, Anthropic: image+document, Gemini: image+document) degradan el bloque a texto vía `missing_attachment_placeholder()` — p. ej. `[image no longer available: shot-accepted.png]` — mismo patrón que los placeholders de bloques no soportados existentes. Fuentes vivas (`file://` existente, `data:`, `https://`) no cambian. El bug estaba en 6 sitios, no 1: el traceback del issue era solo el de `ImageBlock` en OpenAI-compat. Ver `docs/plans/I-119.md`.
* **Problema:**
  1. Enviar una imagen guardada en `/tmp` (p. ej. `file:///tmp/shot-accepted.png`) la persiste en la sesión como `ImageBlock` con esa URI `file://`.
  2. Al recargar la conversación (`--resume`/load), la re-conversión de mensajes para el próximo `llm.stream()` intenta re-leer el archivo, que el SO ya limpió → `FileNotFoundError` y **crash de carga de sesión** (traza: `summarizer._call_with_context_rescue` → `wrap_llm_call` → `_convert_content_block` → `load_file_as_base64`).
* **Causa raíz (verificada en código):** `load_file_as_base64()` (`phoson_llm/utils.py` ~línea 92) hace `open(path, "rb")` **sin chequear `os.path.exists`**; `_convert_content_block()` (`phoson_llm/chats/_openai_compatible.py` ~línea 118) asume que la ruta `file://` sigue viva. Los attachments se crean con `file://{path}` en `phoson_cli/attachments.py` (~líneas 112–124) y se persisten en el historial, pero los archivos de `/tmp` no sobreviven entre runs.
* **Solución propuesta:**
  - En `_convert_content_block()` (o `load_file_as_base64()`): si el archivo ya no existe, degradar el bloque a texto (p. ej. `[image no longer available: shot-accepted.png]`) y emitir `logger.warning` — **sin propagar la excepción**.
  - Verificar que el summarizer (y cualquier middleware que re-lea el historial) no deje propagar el error durante la carga/reintento.
* **Criterio de listo:**
  - Test: sesión con `ImageBlock` `file://` cuyo archivo no existe carga sin crash y el bloque se reemplaza por el placeholder textual.
  - La carga no escribe nada en stderr/traceback.

---

### I-127 — [Feature #127] Bash tool: timeout por invocación (hoy 30s hardcodeado)
* **Estado:** ✅ **Resuelto (v0.17.0)**
* **Área:** `phoson_cli/tools/bash.py`, `phoson_cli/tools/subagent.py`, `phoson_cli/tools/_timeouts.py` (nuevo), `phoson_agent/tool.py`, `docs/api/phoson_cli.md`
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio (mata builds/tests/installs legítimos)
* **Resolución (resumen):** parámetro `timeout` por invocación en la tool `bash` (default 30 s, **sin tope máximo** por decisión del owner: entrenamiento/builds largos son legítimos; el escape ante un hang es cancelar el run con Esc). Extensiones: (a) el mismo control en `agent`/`agents` (omitido → default de config `subagent_timeout_seconds`, `>0` → valor, `0` → sin timeout, inválido → default + nota), y (b) fix en `phoson_agent/tool.py` — el schema perdía la descripción `Annotated` en unions `X | None`. Sanitización compartida en `phoson_cli/tools/_timeouts.sanitize_timeout()` (coerce strings numéricos, rechaza bool/negativos/NaN). Ver `docs/plans/I-127.md`.
* **Problema:**
  1. `DEFAULT_TIMEOUT_SECONDS = 30.0` (`bash.py:22`). Todo comando se mata a los 30s con `Command timed out after 30s`.
  2. `_run_bash()` **ya acepta** `timeout: float` (línea 37) pero el wrapper `@tool bash` (líneas 85–92) no lo expone en el schema: el LLM solo ve `command` + `safe_mode` y no puede reintentar con más tiempo — solo re-ejecutar el mismo comando atascado o pedirle al usuario que lo corra a mano.
  3. Inconsistente: otras partes del CLI sí exponen timeouts configurables (p. ej. `subagent_timeout_seconds`), pero la tool más usada no.
* **Solución propuesta:**
  - Añadir `timeout: float | None = None` a la firma de la tool `bash` y forwardear `timeout or DEFAULT_TIMEOUT_SECONDS` a `_run_bash` (ya soportado internamente).
  - Sanitizar en la capa de tool: `timeout > 0` (con tope sano opcional, p. ej. 3600s); valor inválido → fallback al default con aviso en el resultado.
  - Documentar el parámetro en el docstring (es el que genera el JSON Schema visto por el LLM: "Optional hard timeout in seconds. Defaults to 30s. Increase for long-running builds/tests.").
* **Criterio de listo:**
  - Tests: comportamiento por defecto (30s) inalterado; override explícito se respeta; valores inválidos (≤0) caen al default.
  - El JSON Schema expuesto incluye `timeout` con su descripción.

---

### I-112 — [Bug #112] `UserWarning` de Python impreso a stderr además del warning estilizado del CLI
* **Estado:** ✅ **Resuelto (v0.17.1)**
* **Área:** `phoson_cli/warnings_hook.py` (nuevo), `phoson_cli/__main__.py`, `phoson_cli/fullscreen/app.py`, `phoson_agent/plugins/context_window.py`, `phoson_llm/pricing.py`
* **Prioridad:** **P2** · **Esfuerzo:** S · **Impacto:** 🟡 Medio (ruido visual, expone rutas internas en la TUI)
* **Problema:** Cuando un soft-fail interno emite `warnings.warn(..., UserWarning)` (p. ej. modelo no encontrado en la lista del servidor vLLM), el usuario ve **dos** salidas: el notice estilizado compacto del CLI **y** el warning crudo de Python con archivo+línea a stderr (`.../context_window.py:136: UserWarning: vLLM /v1/models response did not include ...`).
* **Causa raíz (verificada en código):** en clásico/one-shot `main()` no instala ningún hook → el `warnings.showwarning` default imprime a stderr. Además los `logger.warning` de soft-fail caen por `logging.lastResort` a stderr; los 3 except de `context_window.py` emitían `warnings.warn` **y** `logger.warning` (doble). El fullscreen ya lo resolvía con `captureWarnings`+`NullHandler`.
* **Resolución (resumen):** `phoson_cli/warnings_hook.py` instala dos hooks desde `main()` (`try/finally` restore): (1) `showwarning` → notice (stdout, sin `filename`/`lineno`); (2) handler de logging root para `phoson_*` `WARNING+` → mismo notice. El clásico apunta el printer a `Renderer.print_warn` (theme en vivo); one-shot usa el printer plano; fullscreen hace no-op vía `set_fullscreen_active` (el par `NullHandler`+`captureWarnings` se conserva). Dedup en `context_window.py` (se quita el `warnings.warn` redundante de los 3 except; el log del issue #23 se queda). `pricing.py` pierde el advice obsoleto de `filterwarnings`. Ver `docs/plans/I-112.md`.
* **Criterio de listo:**
  - ✅ Test de regresión (capfd): ante un fallo de resolución de context window, **nada** se escribe en stderr y el notice estilizado aparece una sola vez.
  - ✅ Fullscreen intacto (delegación a `captureWarnings` + NullHandler, test existente verde).
  - ✅ Dedup: un soft-fail = un notice. `uv run pytest` (1655) + ruff + pyright limpios.

---

### I-110 — [Feature #110] Plugin system: extender look & commands del CLI, no solo el engine
* **Estado:** ✅ **Resuelto (v0.18.0)** — contrato único `Plugin` ampliado con hooks opcionales y tipos neutrales; `PhosonConfig.plugins` carga specs de comunidad + MCP en interactive/one-shot; catálogo por sesión para slash commands, completion y `/help`; cards de tools con icono/verbo aislados por controller; temas derivados por plugin; `plugin_ui` declarativo (bloques, confirm/select/form con degradación one-shot); y `phoson-cli plugin install|list|enable|disable|remove|update|doctor` con alias `--install-plugin`, inventario `plugins.lock.toml`, pin Git a commit y ejemplo instalable. Ver `docs/plans/I-110.md`.
* **Área:** `phoson_agent/plugin.py`, `phoson_agent/cli_extensions.py`, `phoson_cli/commands.py`, `phoson_cli/formatting.py`, `phoson_cli/theme.py`, `phoson_cli/config.py`, `phoson_cli/controller.py`, `phoson_cli/plugin_manager.py`, `phoson_cli/plugin_ui.py`
* **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟡 Medio (extensibilidad del producto)
* **Problema:** El `Plugin` ABC extiende solo el **engine** (tools + middlewares). El look/commands del CLI están hardcodeados:
  1. `COMMAND_SPECS` en `commands.py` es una tupla estática — un `/my-command` requiere editar el source del CLI.
  2. `_TOOL_VERBS` en `formatting.py` es un dict fijo — una tool nueva de plugin cae al label genérico, sin verbo/icono/render propio.
  3. `theme.py` tiene 4 temas fijos (`frozen` dataclass) — no hay forma de registrar un 5° tema.
  4. `PhosonConfig`/`config.toml` **no tiene campo `plugins`** — el producto no expone los plugins de terceros aunque `AgentEngine(plugins=[...])` los acepte.
* **Solución propuesta (plan del issue, 6 PRs en este orden):**
  1. `aclose()` formal en el `Plugin` ABC (bajo riesgo; hoy es duck-typing implícito en `session_utils.close_plugins`).
  2. `get_commands()` + registro dinámico en `CommandHandler` (nativos primero, luego lo que devuelvan los plugins cargados).
  3. Campo `plugins: list[str | dict]` real en `PhosonConfig`/`config.toml`, mergeado con los MCP en `_rebuild_engine()`.
  4. `register_tool_verb()` público + `get_tool_render_specs()` por plugin.
  5. Temas extensibles vía `get_theme_extension()` (paralelo a `_BY_NAME`).
  6. `docs/plugins.md` + `examples/PLUGIN_EXAMPLES.md` con el "plugin completo" (tool + comando + icono + tema) sin tocar `phoson_cli`.
  - Tipos `CliCommandSpec`/`ToolRenderSpec`/`ThemeExtension` puros/serializables en `phoson_agent` (el render vive en `phoson_cli`); reusar el group de entry points `phoson.plugins` y el patrón de discovery de `skills.py` para temas sin código.
* **Criterio de listo:**
  - Ejemplo de plugin en `examples/` que demuestra los 4 hooks nuevos sin editar nada bajo `phoson_cli`.
  - Los 3 plugins oficiales existentes (`mcp`, `checkpoint`, `memory`) funcionan sin cambios (hooks opcionales con default vacío).

---

### I-126 — [Feature #126] Nuevo plugin oficial: monitores de larga duración que reactivan al agente
* **Estado:** ✅ **Resuelto (v0.19.0, rama `feat/i126-monitors-plugin`)** — paquete `phoson_plugin_monitor/` con kinds `interval`/`file`/`command`, wake por cola persistente + `on_wake`, integración CLI opt-in (`enable_monitors`) y ejemplo host-side. Ver `docs/plans/I-126.md` y `phoson_plugin_monitor/README.md`.
* **Área:** nuevo paquete `phoson_plugin_monitor/` (+ canal de wake en el host)
* **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟢 Bajo (feature de roadmap; desbloquea parcialmente `phoson_http`)
* **Resolución (resumen):**
  - **Scaffold oficial:** `phoson_plugin_monitor/` (`_plugin.py`, `plugin = MonitorPlugin()` en `__init__.py`, `create_plugin()`, README, entrada en "Bundled plugins" de `docs/plugins.md`); empaquetado en la wheel (hatch) y tipado (pyright).
  - **Tools:** `register_monitor(name, kind, spec)` (kind como enum JSON: `interval`/`file`/`command`), `list_monitors()`, `stop_monitor(name)` — schemas vía `@tool`, errores legibles para el LLM.
  - **Kinds:** `interval` (una vez o periódico), `file` (path o glob, polling mtime+size, sin inotify), `command` (salida ≠ 0, timeout con kill del grupo de proceso, o cambio de output; registro gateado por permisos, ejecución unattended documentada).
  - **Wake:** `wake.jsonl` persistente como source of truth (con `session_id` original) + `on_wake` opcional fire-and-forget; dedupe de fires idénticos y cap anti-storm por monitor. El CLI dreana los wakes pendientes en el próximo `run_turn` (header `[MONITOR EVENTS]` en el user message, notificado al sink) y `/monitors` lista estado/wakes pendientes (contrato I-110).
  - **Persistencia/crash:** `monitors.json` + `wake.jsonl` en `data_dir` (default `~/.phoson/monitors/`), writes atómicos (tmp+fsync+replace), parse leniente. El disco es la verdad y las tareas async son caché: `aclose()`/crash no marcan `stopped`, y `ensure_started()` (llamada por el host tras cada rebuild de engine y lazy desde tools) resucita monitores `running`.
  - **CLI (opt-in, core mínimo):** `enable_monitors`/`monitors_data_dir` en config (env `PHOSON_ENABLE_MONITORS`), `build_monitor_plugins()` en `session_utils.py` (in-tree → fallback `path:`), inyección de `session_id_provider` (callable, sobrevive a `new_session`/`load_session`) y drain en `run_turn` — todo duck-typed, cero cambios en `phoson_agent`.
  - **Host example:** `examples/monitor_wake_host.py` — host embebido que reanuda la misma `ConversationTree` (`JsonlStorage`) al despertar.
  - **Tests:** +87 (suite 1714→1801 passing): storage (round-trip, atomicidad, corrupción, cap), kinds con fake clock inyectado, plugin (schemas, lifecycle, resurrección, `/monitors`), e2e con `FakeToolChat` (el LLM registra, el monitor dispara, el host reanuda la misma sesión) e integración CLI (config, provider, drain, rebuild).
* **Problema:** El engine es stateless por run: cuando `AgentEngine.run()` regresa, nada persiste ni se reprograma. No hay mecanismo first-class para "observa X y despiértame cuando pase Y" — hoy requiere host code hackeado.
* **Solución propuesta (sigue el contrato `Plugin` de `docs/plugins.md`):**
  - **Tools** (`get_tools()`): `register_monitor(name, kind, spec)` (kinds: `interval`, `file`/`glob`, `command`, `http`), `list_monitors()`, `stop_monitor(name)`.
  - **Wake:** cola persistente (source of truth) + callback `on_wake` opcional vía `configure()` para hosts en vivo; el evento lleva el `session_id` original para que el host reanud la misma `ConversationTree` (JSONL o `phoson_plugin_checkpoint`).
  - **Persistencia:** registry (definiciones, estado, last-fired) + eventos de wake pendientes en JSON/JSONL — sobreviven reinicios.
  - **Ejecución:** tareas async en el event loop del host (creadas en `initialize()` o lazy); `cleanup()` cancela tareas y procesos; documentar comportamiento al cerrarse el host.
* **Criterio de listo:**
  - Scaffold del paquete con las convenciones de los plugins oficiales (`_plugin.py`, `plugin = MyPlugin()`, README, entrada en "Bundled plugins" de `docs/plugins.md`).
  - ≥2 kinds implementados (p. ej. `interval` + `file`) con schemas JSON vía `@tool`.
  - Mecanismo de wake elegido y documentado con ejemplo host-side en `examples/`.
  - Monitores y wake events persisten entre reinicios; tests unitarios con fakes + 1 integración (patrón skip-if-service-unavailable).
  - Fuera de scope (follow-up): kind `http` (pendiente del daemon `phoson_http`), wake autónomo sin turno del usuario en la TUI, nodo de tree por wake, lock multi-proceso de `data_dir`.

---

### I-115 — [Docs #115] Refrescar README: contenido obsoleto, comprimir sección CLI, assets visuales
* **Estado:** ✅ **Resuelto (PR `i-115-refresh-readme`)** — facts corregidos, sección CLI comprimida a tablas con deep dives en `docs/cli/`, y assets VHS reproducible (`assets/*.tape` → `demo.gif` + `tui.gif`/`tui.png`). Ver `docs/plans/I-115.md`.
* **Área:** `README.md`, `docs/cli/`, `docs/plans/I-115.md`, `assets/`
* **Prioridad:** **P2** · **Esfuerzo:** M · **Impacto:** 🟢 Bajo (calidad/first impression de docs)
* **Resolución (resumen):**
  1. **Facts:** ejemplos sin `phoson_weight` arbitrario (`build_chat()`), features table +8 filas shipped (Sub-agents, Skills, MCP, Plugins, Permissions, Auto-compaction, Standalone binaries), repo map con `examples/`/`bench/`/`scripts/`/`assets/`/`docs/cli/`, CI con los 4 workflows reales, badge PyPI.
  2. **Compresión:** "Interactive CLI" 290→~120 líneas: tabla de flags 1:1 con `_USAGE` (15 flags), tabla de comandos 1:1 con `COMMAND_SPECS` (35/35 nombres), 12 deep dives movidos (texto no reescrito) a `docs/cli/` con índice.
  3. **Visual:** `assets/demo.tape`→`demo.gif` (one-shot real con tool call) y `assets/tui.tape`→`tui.gif`+`tui.png` (hero), generados con VHS contra vLLM local; `assets/README.md` documenta la receta (incl. el truco `env -i SHELL=/bin/bash` para capturar sin el prompt p10k del host).
* **Verificación:** paridad comandos 35/35 y flags 15/15 (script de extracción, 0 faltantes); `vhs validate` OK; frames verificados (typing + salida del agente).
* **Problema:** El README va desfasado (describe ~v0.13.8): lista ~11 comandos cuando la CLI tiene ~35 (faltan `/compact`, `/status`, `/cost`, `/tokens`, `/steps`, `/attach`, `/mcp`, `/permissions`, `/provider`, `/resume`, `/title`, `/delete`, `/env`, `/effort`, …), flags faltantes (`--uninstall`, `--install`), features shipped ausentes (Skills, Sub-agents, MCP, plugins, Permissions, compaction), y ~300 líneas de docs profundas que corresponden a `docs/`.
* **Solución propuesta (3 fases del issue):**
  1. **Facts:** corregir desyncs (tabla de comandos, flags, features, repo map con `examples/`/`bench/`/`scripts/`/`assets/`, CI con `publish.yml`, badges PyPI, Quick Start sin `phoson_weight=1.2` arbitrario).
  2. **Compresión:** "Interactive CLI" → tabla compacta de comandos + flags; mover las explicaciones largas (rewind, Shift+Drag, OSC 8, keybindings, caching, skills, compaction) a `docs/cli/` dejando "why" de 1 línea + link.
  3. **Visual:** hero screenshot de la TUI + demo GIF (~5–8s) generados con **VHS** (`.tape` committed bajo `assets/` para reproducibilidad).
* **Criterio de listo:**
  - Todo comando/flag/feature del README existe en el código (y viceversa para lo shipped).
  - README legible en <5 min; screenshot + GIF renderizados en el preview de GitHub; contenido en inglés (política de idioma).

---

## Detalle de Issues Resueltos (historial)

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
* **Estado:** ✅ **Resuelto (v0.15.0)** — [PR #120](https://github.com/phoson-lat/phoson-engine-minimal/pull/120). Guard de "Esc prefijo" vía `key_processor.input_queue`: cuando el handler eager de `escape` corre, el resto del lote del terminal ya está en la cola; `PhosonApp._is_prefixed_escape()` suprime el Esc solo si la siguiente tecla tiene `data` en 0x20–0x7F (payload Meta, p. ej. `\x7f` de Alt+Backspace). Un 2° Esc real (`\x1b`) o un Ctrl+C ajenos (`\x03`) quedan bajo 0x20 y no se confunden (ver CHANGELOG v0.15.0).
* **Área:** `phoson_cli/fullscreen/app.py`
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio
* **Problema:**
  1. El doble-Esc (rewind) se detecta **solo por tiempo** (`_REWIND_DOUBLE_ESC_WINDOW_SECONDS = 1.0` en `handle_escape()`), no por identidad de tecla.
  2. Muchos terminales codifican **Alt+<tecla>** como `ESC` + <tecla> (encoding Meta/Alt estándar). Para Alt+Backspace, prompt_toolkit expone ese `ESC` como un evento `escape` **indistinguible** de un Esc real.
  3. Consecuencias: con un run en vuelo, el prefijo `ESC` de Alt+Backspace **cancela el agente** (rama "in-flight → cancel"); en idle, arma/completa la ventana de doble-tap y **abre el picker de rewind** sin intención del usuario.
* **Criterio de listo (verificado en PR #120):**
  - Alt+Backspace (o cualquier tecla Alt-modificada con prefijo `ESC`) **no** cancela un run en vuelo ni abre el picker en idle. ✅
  - Doble-Esc deliberado sigue abriendo el picker; single-Esc en vuelo sigue cancelando de inmediato (regresión #68 intacta). ✅
  - Tests de enrutado de teclas vía PipeInput con bytes `ESC`+<key> que no disparan cancel/rewind. ✅

---

### I-109 — [Bug #109] Rewind picker: orden viejo→nuevo e inclusión de entradas no-user (tool results)
* **Estado:** ✅ **Resuelto (v0.15.0)** — [PR #121](https://github.com/phoson-lat/phoson-engine-minimal/pull/121). `jump_candidates()` recorre el path en reversa (nuevo→viejo) y hace el filtro consciente del contenido: un nodo role-`user` solo califica si su contenido es `str` o contiene al menos un `TextBlock` (ver CHANGELOG v0.15.0).
* **Área:** `phoson_cli/controller.py`, `phoson_cli/rewind_picker.py`
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio
* **Problema:**
  1. **Orden:** `SessionController.jump_candidates()` recorre el path activo en orden `root → cursor` y añade los candidatos a medida que avanza, por lo que el picker lista **viejo → nuevo** (índice 1 = turno más antiguo) con el cursor inicial en el más viejo. Debería ser **nuevo → viejo**, con el cursor en el mensaje más reciente (el más probable objetivo de un rewind).
  2. **Contenido:** el filtro es **por role** (`message.role != "user"`), pero el engine guarda los **tool results con role `user`** (`phoson_agent/_tool_runner.py` añade `Message(role="user", content=[ToolResultBlock(...)])`; ver docstring de `ToolResultBlock` en `phoson_llm/schemas/inputs.py`). Esos nodos pasan el filtro y, al no contener `TextBlock`, se renderizan como filas **"(empty message)"** en el picker. (Assistant/system ya están excluidos por el role check.)
* **Criterio de listo (verificado en PR #121):**
  - El picker lista **solo** mensajes del usuario, ordenados **nuevo → viejo**, con el cursor inicial en el más reciente. ✅
  - Conversación con tool calls: el picker no muestra filas "(empty message)" ni nodos de tool results. ✅
  - Tests: `jump_candidates` con un path que mezcla user/assistant/tool-result(user-role) devuelve solo los turnos de usuario genuinos, en orden nuevo→viejo. ✅

---

### I-113 — [Enhancement #113] OpenRouter: orden por `agentic_index` + unificar `/model`/`/provider` en un solo picker con marcado `unavailable`
* **Estado:** ✅ **Resuelto (v0.13.10, hotfix v0.13.11)** — [PR #116](https://github.com/phoson-lat/phoson-engine-minimal/pull/116). OpenRouter ordenado por `agentic_index` desc (sin campo → al final, alfabético; current siempre primero); `list_models_for_providers()` concurrente; picker unificado multi-provider en ambos frontends (selección cambia `(model, provider)` juntos, reusando I-89); providers con fetch fallido marcados `unavailable` en picker y `/model list` (internamente `ModelListingError`; el fast path de 1 provider conserva fallback+warning exactos); docs sin la caché inexistente. Incluye tres fixes de corrección post-review encontrados validando en vivo: (1) `/model <id>` explícito ahora siempre resuelve el provider real vía listing (antes solo lo hacía la rama del picker); (2) se corrigió la ambigüedad del heurístico de prefijo cuando un router (OpenRouter) re-expone el catálogo de otro vendor tal cual; (3) **hotfix v0.13.11** ([PR #117](https://github.com/phoson-lat/phoson-engine-minimal/pull/117)): el lookup de provider estaba gateado en `"/" in id`, así que los ids *sin* prefijo de servidores locales (vLLM/Ollama/LM Studio, p. ej. `Qwen3.8-27B-FP8`) nunca cambiaban el provider — ahora el lookup siempre corre y prefiere un provider distinto al activo cuando varios lo listan (ver CHANGELOG v0.13.10/v0.13.11 y plan `.opencode/plans/i113-model-picker-unified.md`).
* **Área:** `phoson_cli/model_selector.py`, `phoson_cli/model_picker.py`, `phoson_cli/commands.py`, `phoson_cli/command_host.py`, `phoson_cli/fullscreen/command_host.py`, `phoson_cli/fullscreen/model_cache.py`, `phoson_cli/models.py`, `docs/api/phoson_cli.md`, `README.md`
* **Prioridad:** **P2** · **Esfuerzo:** M · **Impacto:** 🟡 Medio
* **Problema:**
  1. **Sin orden útil en OpenRouter.** `_prioritize_current()` ordena todos los proveedores (incluido OpenRouter) por `id.lower()` alfabético, con solo el modelo actual fijado primero. La API de OpenRouter ya devuelve `benchmarks.artificial_analysis.agentic_index` para una parte importante del catálogo, señal mucho más relevante para elegir un modelo orientado a agentes/tool-use que el orden alfabético — y hoy no se usa.
  2. **Selección en dos pasos.** El flujo actual obliga a `/provider` (picker propio) y luego `/model` (que solo lista el proveedor *activo*); no existe una vista única que muestre modelos de todos los proveedores configurados a la vez.
  3. **Fallo de listado silencioso e indistinguible.** Cuando el listado en vivo de un proveedor falla, cada `_list_<provider>_models()` emite un `UserWarning` y degrada a una lista de 1 modelo — indistinguible de un proveedor que legítimamente solo tiene un modelo. No hay marcador visible de `unavailable`.
  4. **Nota aclaratoria:** la caché en disco de `~/.phoson/models.json` para el listado de modelos **ya no existe** — lo único desactualizado era que `docs/api/phoson_cli.md` la describía como si existiera; corregido en este issue.
* **Criterio de listo:** todos verificados en v0.13.10/v0.13.11 (orden `agentic_index`, picker unificado multi-provider, cambio conjunto `(model, provider)`, marcado `unavailable`, fetch concurrente, docs corregidas, tests).

---

### I-100 — [Feature #100] Habilitar / Deshabilitar MCPs a nivel servidor y herramienta
* **Estado:** ✅ **Resuelto (v0.13.12)** — flags `enabled`/`tools` en `mcps.json` (retrocompatibles), `/mcp toggle <server> [tool]` con reapply en vivo, guards de ejecución `ServerDisabled`/`ToolDisabled`, marcado `(disabled)` en `/mcp status` (ver CHANGELOG v0.13.12).
* **Plan de ataque:** ver `.opencode/plans/i100-mcp-server-tool-toggle.md`.
* **Área:** `phoson_plugin_mcp/_plugin.py`, `phoson_cli/_mcp_commands.py`, `phoson_cli/session_utils.py`, `docs/mcp-cli.md`, `mcps.json.example`
* **Prioridad:** **P2** · **Esfuerzo:** M-L · **Impacto:** 🟡 Medio
* **Problema:** No existe forma de desactivar temporalmente un servidor MCP completo o una herramienta MCP específica sin borrar la configuración.
* **Criterio de listo (verificado en v0.13.12):**
  - Servidor desactivado no expone ninguna de sus herramientas. ✅
  - Herramienta específica desactivada se omite del schema enviado al LLM. ✅

---

### I-93 — [Feature #93] Empaquetado binario preconstruido (Linux / macOS / Windows)
* **Estado:** ✅ **Resuelto (v0.15.0)** — [PR #122](https://github.com/phoson-lat/phoson-engine-minimal/pull/122) + hotfixes [PR #124](https://github.com/phoson-lat/phoson-engine-minimal/pull/124) (versión vía env `PHOSON_VERSION`, no flag CLI) y [PR #125](https://github.com/phoson-lat/phoson-engine-minimal/pull/125) (el workflow ya no se dispara en `release:edited` — el attach sube assets al release y eso cuenta como edición → bucle). Release `v0.15.0` publicado con 4 binarios (linux x86_64/arm64, darwin arm64, windows x86_64); macOS Intel pendiente del runner `macos-13` (retirado por GitHub — jobs en cola horas, `actions/runner-images` #13046/#11918). Hotfix v0.16.0: matrix migrada a `macos-15-intel` (label x86_64 soportado hasta ago-2027, #13045) con `continue-on-error` para que la release no bloquee si la cola Intel está vacía (re-dispatch manual posterior).
* **Área:** `.github/workflows/release-binaries.yml`, `phoson_cli.spec`, `phoson_cli/_frozen.py`, `phoson_cli/_views.py`, `phoson_cli/installer.py`, `phoson_cli/updater.py`
* **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟢 Bajo
* **Problema:** Requiere entorno Python ≥3.12 y herramientas de gestión (`uv`/`pip`) para la instalación de usuarios finales.
* **Solución implementada:**
  - `phoson_cli.spec`: entry point `phoson_cli/__main__.py`; data asset `phos-ascii.txt` staged bajo `phoson_cli/`; hidden imports para los SDK de providers/plugins que se importan con lazy-import (gemini, mistral, boto3, mcp, asyncpg, redis, qdrant) + `collect_submodules` de los 6 paquetes propios (el plugin loader los importa dinámicamente); versión inyectada vía `PHOSON_VERSION` (env) a `phoson_cli/_frozen_version.txt` en el bundle.
  - `.github/workflows/release-binaries.yml`: matrix de 5 runners (ubuntu-latest, ubuntu-24.04-arm, macos-latest=arm64, macos-13=x86_64, windows-latest); versión tomada del tag del release (`v0.15.0` → `0.15.0`); build con `uv sync --no-install-project --all-extras` + `pyinstaller phoson_cli.spec` con `env: PHOSON_VERSION`; job `attach` que renombra los artifacts a los nombres de la tabla del README y los sube con `softprops/action-gh-release` (solo en evento `release:published`).
  - `phoson_cli/_frozen.py`: `asset_path()` resuelve assets en `sys._MEIPASS/phoson_cli/` (bundle) o junto al módulo (source); `is_frozen()` desde `sys.frozen`; `frozen_version()` lee `_frozen_version.txt` inyectado en build.
  - `updater.py`: nuevo `InstallMode.FROZEN` (detectado primero); `get_current_version()` usa `frozen_version` cuando está congelado; `manual_hint` para frozen apunta a la página de Releases.
  - README: sección "Standalone binaries (no Python required)" con la tabla de assets.
* **Criterio de listo:**
  - Binarios autónomos descargables desde la sección de Releases de GitHub. ✅ (release v0.15.0 con 4 de 5 plataformas)
  - `phoson-cli --version` funciona dentro del binario (versión inyectada, sin metadata). ✅
  - `test_frozen_unit.py`: 12 tests de asset_path (source + MEIPASS), is_frozen, frozen_version, integración updater (modo FROZEN). ✅

---

## Roadmap sugerido de ataque

```
───── Resueltos ─────

Sprint Extensibilidad & Ecosistema
├── I-110 (Plugin system: look & commands del CLI) ✅ v0.18.0
└── I-126 (Plugin de monitores de larga duración) ✅ v0.19.0

Sprint Docs
└── I-115 (Refresh del README + docs/cli/ + assets VHS) ✅ PR i-115-refresh-readme

Sprint Robustez & Confiabilidad
├── I-128 (Feedback en vivo mientras el modelo compone la tool call) ✅ v0.16.0
├── I-119 (Crash al cargar sesión con attachments temporales borrados) ✅ v0.16.1
├── I-127 (Timeout por invocación en bash + sub-agents) ✅ v0.17.0
└── I-112 (UserWarning duplicado a stderr + notice estilizado) ✅ v0.17.1

Sprint Estabilidad de Contexto & Métricas
├── I-91 (Auto-compact gate + fallback 400) ✅ v0.13.5
├── I-88 (Header live metrics + OpenRouter USD cost) ✅ v0.13.6
└── I-89 (/model persiste provider en config.toml) ✅ v0.13.7

Sprint UX & Performance
├── I-108 (Alt+Backspace no debe leerse como doble-Esc / cancel) ✅ v0.15.0
├── I-109 (Rewind picker: orden nuevo→viejo y solo mensajes user) ✅ v0.15.0
├── I-83 (Compactar paneles de error a 1 línea en reintentos) ✅ v0.13.8
├── I-84 (Optimización de CPU en idle/streaming) ✅ v0.13.9
└── I-82 (vLLM Qwen3.x) ✅ Cerrado — error de vLLM, no del engine

Sprint Ecosistema & Distribución
├── I-113 (OpenRouter agentic_index sort + picker unificado /model+/provider) ✅ v0.13.10 + hotfix v0.13.11
├── I-100 (Toggle granular MCP servers & tools) ✅ v0.13.12
└── I-93 (Binarios precompilados standalone en CI) ✅ v0.15.0
```

## Principios de desarrollo

1. **Mantener paridad entre frontends:** Cualquier render nuevo debe ser una función pura en `formatting.py` utilizable en modo fullscreen y clásico.
2. **Cobertura de tests rigurosa:** Cada corrección o feature debe incluir tests unitarios/e2e y pasar validación estricta de `ruff` y `pyright`.
3. **Optimización con métricas:** Todo cambio de performance (CPU, tokens, tiempo) debe incluir benchmark o medición verificable.
