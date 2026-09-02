# REVISION FINAL BY FABLE — phoson-engine-minimal / phoson-cli

> **Generado por:** Claude Fable 5.1 (Anthropic), sesión de Claude Code
> **Fecha:** 2026-09-01
> **Versión analizada:** v0.23.0 + rama `feat/tui-perf-windowing` (HEAD `323de8c` + diff sin commitear) · 1937 tests recolectados · ruff/pyright limpios según CI
> **Metodología:** 4 subagentes de auditoría directa de código (loop del agente, tools, capa CLI/config, TUI) + verificación manual en código de cada hallazgo marcado ✅ + cruce contra los cinco reportes previos del repo.
> **Documentos cruzados:** `REVISION-BY-ANTIGRAVITY.md`, `reporte-harness.md`, `IMPROVEMENTS.md`, `IMPROVEMENTS-TUI.md`, `ISSUES-COMPLEXITY.md`.
> **Idioma:** español, siguiendo la convención de los documentos de plan interno.

---

## 0. Cómo leer este documento

- **§1** es el veredicto en una página.
- **§2** es la tabla maestra: cada hallazgo con ID `F-nn`, archivo:línea, severidad, quién lo reportó y si está verificado en código.
- **§3** es el cruce: qué confirma, qué corrige y qué añade esta revisión respecto a los reportes anteriores. Es la parte que justifica que exista este archivo.
- **§4** compara contra el SOTA de harness CLI (Claude Code, Codex CLI, Gemini CLI, OpenCode, Aider) sin estrellas: cada celda dice qué existe y dónde.
- **§5** detalla por capa.
- **§6** es el plan de ataque unificado con `ISSUES-COMPLEXITY.md`.
- **§7** son límites de la revisión: lo que no se verificó.

Convenciones de estado: **✅ verificado** = leído y confirmado en el código por esta revisión; **⚠️ reportado** = lo afirma un subagente o un reporte previo y no se re-verificó línea a línea; **🔁 ya trackeado** = existe issue/ítem H-*/I-*/T-*.

Severidad: 🔴 alta (seguridad, corrupción de historial, pérdida de turno o bug visible en uso normal) · 🟠 media · 🟡 baja / perf / deuda.

---

## 1. Veredicto

phoson-cli es un **harness completo** según la definición formal (loop + interfaz de tools + gestión de contexto + mecanismos de control), y varias de sus decisiones están al nivel de los mejores: skills con progressive disclosure sin romper el prompt cache, AGENTS.md en el system prompt, gate de compactación que cuenta schemas y reserva de output, offload de resultados a disco, backfill de `tool_result` al cancelar, pooling MCP medido, árbol de conversación con escritura atómica, y un TUI con windowing O(visible) y command palette. La cobertura de tests (1937) y la disciplina documental (ADRs, planes por issue, CHANGELOG detallado) son inusuales para un proyecto de este tamaño.

Los reportes previos coinciden en que **el gap principal es la ausencia de función de fitness (H-1) y trazas (H-2)**. Esta revisión no discute esa tesis. Lo que añade es una capa que ninguno de los reportes vio porque o no leyeron código (`reporte-harness.md`) o lo leyeron de forma descriptiva (`REVISION-BY-ANTIGRAVITY.md`): **hay bugs de corrección y seguridad verificables hoy, que no necesitan un gate de no-regresión para justificar su arreglo**, y varios contradicen afirmaciones de esos reportes.

Los tres bloques que más importan, en orden:

1. **Fronteras de seguridad inconsistentes.** Los sub-agentes (`agent`/`agents`) y el modo one-shot (`-p`) construyen un `AgentEngine` **sin** `PermissionMiddleware`. La afirmación "permisos que fallan cerrado en no-interactivo" (reporte-harness, IMPROVEMENTS "✅ Exacto", Antigravity §1.4) es falsa en los dos caminos donde más importa. Además, el matching de allow-patterns es `fnmatch` sobre el comando completo, así que `git *` aprueba `git status; rm -rf /` (Antigravity V-01, confirmado).
2. **Robustez del loop.** La compactación corta el historial por número de mensajes sin respetar pares `tool_use`/`tool_result`, produciendo un 400 que el rescate de emergencia no reconoce. El retry existe en dos implementaciones, una no reintenta nunca y ninguna está conectada en el CLI. `stop_reason` no se lee en ningún adaptador.
3. **Primitivas de edición y navegación.** `patch_file` reemplaza la primera ocurrencia sin verificar unicidad. No hay Grep ni Glob nativos. `read_file` no devuelve números de línea. El system prompt tiene ~60 palabras y no guía el uso de tools. Con modelo fijo, esto es lo que más mueve la tasa de éxito, y es exactamente el tipo de cambio que H-1 debería medir.

Un bug de la rama actual (F-40, ventana estancada del windowing en `323de8c`) se detectó y quedó corregido durante la revisión (`40c8022`, PR #173); se conserva en la tabla porque ilustra el tipo de defecto que ninguna de las revisiones anteriores captó.

---

## 2. Tabla maestra de hallazgos

### 2.1 Seguridad y permisos

| ID | Hallazgo | Ubicación | Sev. | Fuente | Estado |
|---|---|---|---|---|---|
| **F-01** | Sub-agentes corren sin `PermissionMiddleware` ni `safe_mode`: `AgentEngine(chat, tools, max_iterations)` con contexto vacío y `middlewares=[]`. Un tool en `deny` sigue siendo invocable desde un sub-agente. El comentario `# propagated via context` es falso. | `phoson_cli/tools/subagent.py:424-428, 664-668`; middleware solo en padre: `controller.py:413` | 🔴 | Fable | ✅ verificado · **nuevo** · issue #174 |
| **F-02** | One-shot (`-p`) construye el engine sin Permission, Summarizer ni Offload. `permissions.json` no se consulta, no hay autocompactación, y `print(result.final_content)` imprime `None` con contenido vacío. | `phoson_cli/__main__.py:370-375, 401` | 🔴 | Fable | ✅ verificado · **nuevo** · contradice reporte-harness/IMPROVEMENTS/Antigravity · issue #174 |
| **F-03** | Allow-patterns con `fnmatch` sobre la cadena completa: `git *` hace match con `git status; rm -rf /`. | `phoson_agent/permissions.py:89-93` | 🔴 | Antigravity V-01 | ✅ verificado · roza H-6 pero no está en su alcance · issue #175 |
| **F-04** | `@import` en AGENTS.md/CLAUDE.md resuelve rutas absolutas fuera del repo (`@/home/u/.ssh/id_rsa` entra al system prompt enviado al proveedor). `~` no se expande. | `phoson_cli/agents_md.py:88` | 🟠 | Fable | ✅ verificado · **nuevo** · issue #182 |
| **F-05** | Sin confinamiento de rutas en `read_file`/`write_file`/`patch_file`/`list_dir`/`view_image` (absolutas y `..` aceptadas) con permiso `allow` por defecto. | `phoson_cli/tools/files.py:21,48,63,86`; `view_image.py:20` | 🟠 | Fable | ⚠️ reportado · Claude Code tampoco confina, pero pide confirmación fuera del cwd |
| **F-06** | `web_fetch` sin filtro SSRF (metadata endpoints, localhost, hosts internos), sigue redirects, y bufferea el cuerpo completo antes del cap de 50 KB. El modelo no recibe aviso de que el contenido es no confiable aunque el docstring del módulo lo reconoce. | `phoson_cli/tools/web_fetch.py:12-14, 104-122` | 🟠 | Fable | ⚠️ reportado · issue #183 |
| **F-07** | Para tools sin `match_args` (todos menos `bash`), el texto que se matchea contra allow-patterns es "el primer argumento string en orden de dict", orden que controla el modelo. | `phoson_agent/permissions.py:144-146` | 🟡 | Fable | ✅ verificado · issue #175 |
| **F-08** | `.claude/skills` de un checkout no confiable se cargan y se enmarcan como "take precedence over your defaults". Prefix matching en nombres de skill. | `phoson_cli/skills.py:344, 458-460` | 🟡 | Fable | ⚠️ reportado |

### 2.2 Corrección del loop del agente

| ID | Hallazgo | Ubicación | Sev. | Fuente | Estado |
|---|---|---|---|---|---|
| **F-10** | La compactación corta en `others[-min_keep_messages:]` sin mirar bloques. Cada tool call ocupa 2 mensajes (3 con imagen), así que la cola arranca a menudo con un `tool_result` huérfano. Anthropic responde 400 "tool_result without tool_use"; como no es error de contexto, `_call_with_context_rescue` no lo reconoce y el turno muere. | `phoson_agent/plugins/summarizer.py:582, 777, 987` | 🔴 | Fable | ✅ verificado · **nuevo** · contradice Antigravity "compactación robusta" · issue #176 · **resuelto** (PR en curso): `safe_cut_index` en los 4 cortes + 400 de pairing ⇒ error explícito. |
| **F-11** | Si el resumen viene vacío (p. ej. el modelo responde con un tool call, porque `call_next` pasa la lista de tools), se omite el mensaje de resumen pero se devuelve `compacted` igual: el historial medio desaparece sin rastro. | `summarizer.py:608-614, 627-641`; `_internals.py:165` | 🟠 | Fable | ⚠️ reportado · issue #176 · **resuelto** (PR en curso): resumen vacío ⇒ abortar; llamada de resumen tool-free vía `chat`. |
| **F-12** | **Retry inexistente en la práctica.** `RetryMiddleware` marca `visible_event_seen=True` con cualquier evento no-error y todos los adaptadores emiten `LLMStartEvent` primero, así que nunca reintenta. `RetryingChat` (correcto) no está conectado en `build_chat`. Solo se exporta en `__init__`. Un 429/529 o una caída de conexión matan el turno. | `phoson_agent/middleware.py:93-100`; `phoson_llm/chats/anthropic.py:368`; grep negativo en `phoson_cli` | 🔴 | Fable | ✅ verificado · **nuevo** · Antigravity §2.5 describe `RetryingChat` sin notar que no se usa · issue #177 |
| **F-13** | `stop_reason` no se lee en ningún adaptador. Un `max_tokens` truncado es indistinguible de una respuesta completa; un tool call JSON truncado llega al handler como `fn(_raw=...)` y explota con `TypeError` opaco. `refusal`/`pause_turn` sin manejar. | grep negativo en `phoson_llm` y `phoson_agent`; `anthropic.py:~437` | 🟠 | Fable | ✅ verificado · **nuevo** · issue #178 |
| **F-14** | Solo `PhosonAgentError` desde `on_before_tool` se captura. Cualquier otra excepción de middleware o del adaptador escapa del `stream()` y deja un `tool_use` sin `tool_result` en el historial (el backfill solo corre para `CancelledError`). | `phoson_agent/_tool_runner.py:119-121`; `anthropic.py:478-495` | 🟠 | Fable | ⚠️ reportado · matiza Antigravity "nunca quedan tool_use huérfanos" (cierto solo para cancelación) · issue #178 |
| **F-15** | Costo de compactación se pierde: `_consume_llm_stream` conserva solo el último `UsageEvent`; `_emergency_compact` descarta `_fwd`. `total_cost_usd` subestima cada compactación. | `phoson_agent/_loop.py:250-251`; `summarizer.py:807` | 🟡 | Fable | ⚠️ reportado |
| **F-16** | Handlers sync se invocan inline: bloquean el event loop y difieren la cancelación. Sin timeout genérico por tool. | `_tool_runner.py:228` | 🟡 | Fable | ⚠️ reportado |
| **F-17** | Texto parcial streameado se descarta en `IterationFailed`; la sesión persistida no tiene lo que el usuario vio. `max_iterations` agotado termina con error sin turno de cierre. | `_loop.py:160-168`; `agent.py:398-407` | 🟡 | Fable | ⚠️ reportado |
| **F-18** | Ejecución de tools estrictamente secuencial, incluso read-only. | `_tool_runner.py:91` | 🟡 perf | Fable + Antigravity V-04 | ✅ verificado |
| **F-19** | Sin validación de schema de argumentos: `fn(**args)` directo. | `phoson_agent/tool.py:186-200` | 🟡 | Fable | ⚠️ reportado |

### 2.3 Tools y system prompt

| ID | Hallazgo | Ubicación | Sev. | Fuente | Estado |
|---|---|---|---|---|---|
| **F-20** | `patch_file` con `replace_all=False` hace `replace(old, new, 1)` y reporta `count=1` sin verificar unicidad: con ancla ambigua edita el sitio equivocado en silencio. Sin guard read-before-edit. | `phoson_cli/tools/files.py:76-77` | 🔴 (tasa de éxito) | Fable | ✅ verificado · **nuevo** · Antigravity §3.4 lo describe como correcto · issue #179 |
| **F-21** | Sin Grep ni Glob nativos; la única navegación es `list_dir` (profundidad 3, sin tope, sin `.gitignore`) y `bash`. `read_file` no devuelve números de línea. | `files.py:85-109, 113` | 🟠 gap SOTA | Fable | ✅ verificado · **nuevo** · issues #180, #181 |
| **F-22** | Salidas sin tope: `list_dir`, `read_file` con rango, `agent`/`agents` (texto del sub-agente verbatim). El cap de 50 KB de `read_file` compara bytes pero recorta caracteres. | `files.py:29-30, 44, 92-109`; `subagent.py` | 🟡 | Fable | ⚠️ reportado · issue #180 |
| **F-23** | Sub-agentes sin system prompt: `messages=[Message(role="user", content=task)]`. No conocen cwd, fecha, AGENTS.md ni reglas. | `subagent.py:429, 670` | 🟠 | Fable | ✅ verificado · **nuevo** · issue #184 |
| **F-24** | `agents` se anuncia a los sub-agentes (solo se quita `agent`) pero siempre falla con `TypeError` porque el contexto hijo no tiene `chat`/`available_tools`. La recursión está acotada por accidente. | `subagent.py:259` | 🟡 | Fable | ⚠️ reportado · issue #184 |
| **F-25** | Descripciones de tools son docstrings de una línea sin "cuándo usar / cuándo no / ejemplos" (`patch_file`, `read_file`, `write_file`). System prompt base de ~60 palabras: sin guía de uso de tools, sin git status/branch, sin reglas de seguridad, sin formato de respuesta, sin aviso de contenido no confiable en `web_fetch`. | `files.py:116,122,130`; `phoson_cli/session_utils.py:32-39, 83-140` | 🟠 | Fable | ✅ verificado · **nuevo** · issue #180 |
| **F-26** | `read_file`/`patch_file` con UTF-8 estricto: binarios o Latin-1 devuelven `UnicodeDecodeError` crudo. `patch_file` no normaliza CRLF. | `files.py:25, 67` | 🟡 | Fable | ⚠️ reportado |

### 2.4 Capa CLI / config / sesiones

| ID | Hallazgo | Ubicación | Sev. | Fuente | Estado |
|---|---|---|---|---|---|
| **F-30** | Ctrl-C durante un run en el REPL clásico: bajo `asyncio.run` (3.11+) SIGINT cancela la main task; el `CancelledError` lo traga `controller.py:912` y el REPL sigue con la task en estado `cancelling()`. Segundo Ctrl-C crashea. Sin test. | `phoson_cli/repl.py:528`; `controller.py:912` | 🟡 | Fable | ⚠️ reportado |
| **F-31** | `run_turn`/`_execute_turn` y los handlers nativos de comandos no tienen `except Exception`; en el REPL clásico un error de render o `OSError` al guardar crashea sin `shutdown()` (fugan subprocesos MCP). | `controller.py:657`; `commands.py:672`; `repl.py:519/527`; `__main__.py:529` | 🟡 | Fable | ⚠️ reportado |
| **F-32** | `_rebuild_engine`: `create_task` sin retener referencia para `aclose()`/`close_plugins()`; si `build_chat` o `AgentEngine(...)` fallan a medio rebuild, `config.provider/model` ya mutaron sin rollback. `_cmd_model` no captura `ValueError`. | `controller.py:370-415, 1329, 1345-1347`; `commands.py:842/852` | 🟡 | Fable | ⚠️ reportado |
| **F-33** | Non-TTY stdin sin task (`phoson-cli </dev/null`) arranca el TUI; stdin se consume antes de evaluar `--version/--setup`. | `__main__.py:173-177, 218` | 🟡 | Fable | ⚠️ reportado |
| **F-34** | `/resume` escribe `meta.total_tokens` (input+output) en `total_output_tokens`. | `controller.py:1270`; `storage_jsonl.py:80` | 🟡 | Fable | ⚠️ reportado · issue #185 |
| **F-35** | `/compact` manual no persiste la sesión ni refresca `_context_tokens`; salir justo después lo pierde. `/new`/`/resume` no comprueban `is_running` (el wake del monitor puede estar a medio turno). | `controller.py:1167-1255`; `commands.py:874` | 🟡 | Fable | ⚠️ reportado · issue #185 |
| **F-36** | Config: `_resolve_bool` hace `bool(fd[key])` así que `"false"` es verdadero; `_resolve_int` sin try (traceback crudo); `save_config` completo persiste API keys venidas de env al archivo. | `phoson_cli/config.py:219, 231, 739-759` | 🟡 | Fable | ⚠️ reportado · issue #185 |
| **F-37** | `/mcp config <path>` sin `expanduser`; `toggle_mcp_config` reescribe `mcps.json` sin restaurar permisos aunque tenga secretos; `/mcp toggle` de un stdio server rebuild completo (respawn de todos). | `phoson_cli/_mcp_commands.py:364, 98, 323` | 🟡 | Fable | ⚠️ reportado · issue #185 |
| **F-38** | `run_upgrade_command` sin timeout (puede colgar el REPL); check diario a PyPI sin opt-out; `plugin list` siempre dice "enabled"; `disable` de un plugin `path:` borra el spec y no se puede re-enable. | `phoson_cli/updater.py:265`; `plugin_manager.py:292` | 🟡 | Fable | ⚠️ reportado · issue #185 |

### 2.5 TUI (fullscreen)

| ID | Hallazgo | Ubicación | Sev. | Fuente | Estado |
|---|---|---|---|---|---|
| **F-40** | Ventana estancada en `323de8c`: `_render_chat` re-sliceaba solo si cambiaban `(top, height, total)`, así que un tick de spinner, un token en la misma línea o un cambio de tema devolvían la ventana vieja. **Corregido en `40c8022` (PR #173)** con `_chat_content_epoch` bumpeado en cada re-render dirty y añadido a la clave del slice; test de regresión `test_spinner_tick_refreshes_same_position_window`. | `phoson_cli/fullscreen/app.py:443, 1021, 1043` | 🔴→✅ | Fable | ✅ verificado · **corregido durante la revisión** · contradice Antigravity TUI ⭐⭐⭐⭐⭐ en el estado que Antigravity evaluó |
| **F-41** | El fingerprint del prefijo `(width, *id(block))` no cambia con `apply_theme` (que vacía `BlockAnsiCache`) ni con `_reset_transcript`; `id()` puede reciclarse tras `blocks.clear()`. Riesgo latente de bounds incorrectos con temas de escapes de distinta longitud. **Fix:** contador de generación bumpeado en `BlockAnsiCache.clear`. | `app.py:969-972, 694` | 🟡 | Fable | ✅ corregido en **PR #190** (`generation` en `BlockAnsiCache`, primer elemento del fingerprint; `test_apply_theme_invalidates_frozen_bounds_cache`) · issue #186 |
| **F-42** | ANSI/OSC crudo en cuerpos de `bash`: `Text(stdout)` no strippea ESC; ptk renderiza `\x1b]0;title\x07` como texto literal. `ls --color`, `git` con color, scripts que setean título → basura visible. **Fix:** `Text.from_ansi` / `strip_control_codes`. | `phoson_cli/formatting.py:692` | 🟠 | Fable | ✅ corregido en **PR #190** (`_OSC_RE` + `Text.from_ansi`; `test_bash_body_strips_osc_and_keeps_sgr_as_style`) · issue #186 |
| **F-43** | Llamadas bloqueantes en el render path: `shutil.get_terminal_size` por frame, `load_policy()` (cache 1 s) y `collect_agents_md_files()` (cache 5 s) dentro del render del header; `save_config`/`save_policy` sync en keypress; clipboard `_run_command` sin timeout y temp files nunca limpiados. | `app.py:985, 881, 916, 1569, 1615`; `clipboard.py:740` | 🟡 | Fable | ⚠️ reportado |
| **F-44** | La descripción "O(visible)" del CHANGELOG/docstring está sobreestimada: cada frame dirty sigue escribiendo todos los bloques cacheados en un buffer y copiando `text[:prefix_len]`/`text[prefix_len:]` antes del check de fingerprint. Son memcpys en C, así que la ganancia real es eliminar el loop Python de `str.find`, pero conviene decirlo con precisión. | `render.py:183-184, 245`; `app.py:967-968` | 🟡 docs | Fable | ✅ corregido en **PR #190** (docstring `_compute_chat_bounds` + CHANGELOG v0.24.0) · issue #186 |
| **F-45** | `app.py` (~2000 líneas) es un god object: layout, scroll, header/footer, windowing, submit, palette, `!` bash, 5 floats, rewind, ciclos de permiso/effort, clipboard, lifecycle. Candidatos de extracción: `ChatPane`, `HeaderModel`, `floats.py`, `RewindController`. `SessionController` (1520) y `commands.py` (1596) son los siguientes. `CommandHost` existe duplicado (`command_host.py` y `fullscreen/command_host.py`). | — | 🟡 deuda | Fable | ⚠️ · no trackeado en IMPROVEMENTS-TUI · issue #187 |

### 2.6 Adaptadores LLM y plugins

| ID | Hallazgo | Ubicación | Sev. | Fuente | Estado |
|---|---|---|---|---|---|
| **F-50** | Adaptadores incompletos: Bedrock envuelve `client.converse` sync en `run_in_executor` (sin streaming real); Mistral tiene `tools=` comentado; Gemini asume `index=0  # Simplified` en tool calls; Ollama no extrae `<think>`. | `phoson_llm/chats/bedrock.py:86-88`; `mistral.py:70`; `gemini.py:220`; `ollama.py` (grep negativo) | 🟡 | Antigravity §2.4 | ✅ verificado por grep · issue #188 |
| **F-51** | `~/.phoson/compacted/` sin retención, TTL ni cuota (el docstring dice "cleanup is a `rm -rf` away"). | `phoson_agent/plugins/offload.py:19` | 🟡 | Antigravity V-02 | ✅ verificado · issue #189 |
| **F-52** | `PostgresStorage.save` hace `DELETE` + `INSERT` de todos los nodos en cada guardado: O(N) por step. | `phoson_plugin_checkpoint/storage.py:143-160` | 🟡 perf | Antigravity V-05 | ✅ verificado · issue #189 |
| **F-53** | Token drift de `cl100k_base` frente a tokenizadores reales (10-25 % en código no-ASCII). Mitigado por el margen del 10 % y el aprendizaje de la ventana real desde 400s. | `summarizer.py:57-62` | 🟡 | Antigravity V-03 | ⚠️ reportado · opinión razonable, no medida aquí |

### 2.7 Ya trackeado en el repo (confirmado, sin novedad)

| Ítem | Estado verificado |
|---|---|
| H-0 bench `--model/--provider` (#138) | ✅ Corregido en código (`bench/run_bench.py` usa `PHOSON_MODEL`/`PHOSON_PROVIDER`) y marcado resuelto en IMPROVEMENTS/ISSUES-COMPLEXITY, **pero el issue sigue OPEN en GitHub**. Cerrar. |
| H-1 eval set + gate nightly (#139) | ✅ Sigue abierto. `bench/` tiene **4 tareas** triviales (`fix_failing_script` es cambiar `-` por `+`), `bench/results/` vacío, ningún workflow lo invoca. |
| H-2 OTel (#140) | ✅ Sigue abierto. Grep negativo de `otel`/`opentelemetry` en todos los paquetes. |
| H-3 doom loops (#142), H-4 contexto ambiental (#143), H-5 sandwich (#145), H-7 wall-clock one-shot (#141), H-9 compact tool (#147), H-11 tool budget (#148), H-10 handoff (#149) | ✅ Todos abiertos, diagnósticos correctos. |
| H-6 permisos por intención (#144) | ✅ Abierto. **Nota:** F-01/F-02/F-03 son bugs del sistema *actual*, no requieren la taxonomía de intención de H-6 y deberían arreglarse antes. |
| I-134 preserved thinking | ✅ Confirmado: `_build_assistant_message` solo emite `TextBlock`+`ToolUseBlock`. |
| I-129 background agents | Sin verificar, es feature. |
| T-1…T-13 (look) | ✅ Todos shipped (v0.20.0–v0.23.0). T-11 ADR cerrado. |
| I-127 bash timeout sin tope | ✅ `bash` siempre tiene timeout y mata el proceso (`bash.py:80-90`); el "sin tope" es sobre el máximo permitido, decisión del owner. |

---

## 3. Cruce con los reportes anteriores

### 3.1 Respecto a `REVISION-BY-ANTIGRAVITY.md`

**Lo que acierta y esta revisión confirma en código:** V-01 fnmatch (F-03), V-02 offload sin retención (F-51), V-04 tools secuenciales (F-18), V-05 Postgres O(N) (F-52), la tabla de madurez de adaptadores (F-50), el diagnóstico de I-134, y la tesis H-1/H-2 primero. Su descripción de la arquitectura del loop, del gate de compactación de tres niveles y del prompt caching de tres breakpoints es precisa y útil.

**Lo que afirma y el código contradice:**

| Afirmación de Antigravity | Realidad verificada |
|---|---|
| §1.4 "`PermissionMiddleware` implementa el principio fail-closed"; permisos ⭐⭐⭐⭐⭐ implícito | El middleware es correcto, pero **no está adjunto** en sub-agentes (F-01) ni en one-shot (F-02). Fail-closed que no se ejecuta no protege. |
| §1.1 "Nunca quedan bloques `tool_use` huérfanos en el historial" | Cierto para `CancelledError`. Falso para cualquier otra excepción (F-14) y para la compactación (F-10), que los crea activamente. |
| §1.2 "compactación robusta", tabla resumen "compactación robusta" | El gate es robusto. El **corte** no respeta pares tool_use/tool_result (F-10) y un resumen vacío borra el historial (F-11). |
| §2.5 `RetryingChat` "implementa un contrato formal de no-duplicación" | Correcto como código, pero **no se usa en ningún sitio** del CLI; el `RetryMiddleware` que sí se exporta nunca reintenta (F-12). |
| §3.4 `patch_file`: "búsqueda exacta de chunks, `str.replace` con verificación de presencia" | Verifica presencia, **no unicidad** (F-20). Es la diferencia entre un edit tool seguro y uno que edita el sitio equivocado. |
| §3.2 / tabla "CLI & TUI ⭐⭐⭐⭐⭐ · O(viewport) windowing" | El windowing tiene un bug de ventana estancada en HEAD (F-40) y el "O(visible)" está sobreestimado (F-44). |
| §5.2 tabla comparativa con estrellas | Autorreferencial (columna "Antigravity"), y varias celdas no son evidenciables: "Claude Code ❌ solo Anthropic" ignora Bedrock/Vertex; "topología lineal" en Claude Code ignora `--resume`/fork; "TUI Phoson ⭐⭐⭐⭐⭐ > Claude Code ⭐⭐⭐⭐" no resiste F-40/F-42 ni la ausencia de diff preview, cola de mensajes y Ctrl-R. Esta revisión sustituye estrellas por "existe / parcial / falta + dónde" (§4). |

**Lo que Antigravity omite por completo:** la seguridad de sub-agentes, el modo one-shot, `stop_reason`, `@import` fuera del repo, SSRF, la calidad del system prompt y de las descripciones de tools, y la ausencia de Grep/Glob. Es decir, toda la capa "interfaz agente-computadora" que en la literatura de SWE-agent explica los deltas más grandes con modelo fijo.

### 3.2 Respecto a `reporte-harness.md` e `IMPROVEMENTS.md` §B

La tesis central (sin H-1 todo es apuesta) se sostiene y esta revisión la asume. Correcciones:

- **"Permisos que fallan cerrado en no-interactivo"** (reporte-harness, "Diagnóstico honesto"; IMPROVEMENTS §B marca "✅ Exacto"): incorrecto para one-shot (F-02). El único fail-closed que sobrevive en `-p` es el `safe_mode` de `bash` vía `context.extra`, y `safe_mode` está apagado por defecto (`config.py:83`).
- **H-7 (wall-clock en one-shot)** sigue siendo válido, pero F-02 lo hace más urgente: un run `-p` no tiene ni permisos ni compactación ni tope de tiempo.
- **H-6 (permisos por intención)** propone una taxonomía nueva. F-03 muestra que el mecanismo actual tiene un bypass trivial que se arregla con `shlex.split` + match sobre el primer token, sin esperar a H-6.
- **"Cada componente del harness existe porque asumes que el modelo no puede algo"**: aplica también en sentido inverso. F-20/F-21/F-25 son casos donde el harness asume que el modelo *sí* puede (anclar un edit único sin verificación, navegar sin grep, saber cómo usar tools sin guía) y la evidencia de SWE-agent dice que no es seguro asumirlo.

### 3.3 Respecto a `IMPROVEMENTS-TUI.md` e `ISSUES-COMPLEXITY.md`

Todo el sprint de look (T-1…T-13) está shipped y no se re-evalúa aquí. Lo que estos documentos no trackean y esta revisión añade: F-40, F-41, F-42, F-43, F-45. Ninguno es de look; son corrección y deuda.

La regla 5 de `ISSUES-COMPLEXITY.md` ("todo lo que diga *medido contra H-1* no está listo hasta que #139 exista") es correcta para hipótesis de harness (doom loops, sandwich, budget en contexto). **No aplica a bugs.** F-01/F-02/F-03/F-10/F-12/F-20/F-40 no son hipótesis sobre el comportamiento del modelo; son defectos con reproducción determinista. §6 propone dónde encajan.

### 3.4 Hallazgos que ningún reporte previo tenía

F-01, F-02, F-04, F-05, F-06, F-07, F-10, F-11, F-12, F-13, F-14, F-15, F-17, F-19, F-20, F-21, F-22, F-23, F-24, F-25, F-26, F-30…F-38, F-40…F-45.

---

## 4. Comparación con el SOTA de harness CLI (2026)

Referencias: Claude Code, Codex CLI, Gemini CLI, OpenCode, Aider. Sin estrellas: cada celda dice qué hay y dónde.

| Dimensión | phoson-cli (verificado) | Referencia SOTA | Gap |
|---|---|---|---|
| **Loop y cancelación** | ReAct propio; tools secuenciales; backfill de `tool_result` al cancelar (`_tool_runner.py:91-103`) | Paralelo para tools read-only; cancelación con backfill | Medio (F-18) |
| **Edición de archivos** | `patch_file` sin unicidad ni read-before-edit; `read_file` sin números de línea; `write_file` sin aviso de sobreescritura | Claude Code: Edit exige ancla única + lectura previa, Read con `cat -n`. Codex: `apply_patch` multi-hunk. Aider: diff/udiff con repo map | **Alto** (F-20, F-21) |
| **Navegación de código** | `list_dir` (profundidad 3) + `bash` | Glob + Grep nativos con semántica ripgrep, resultados estructurados | **Alto** (F-21) |
| **System prompt** | ~60 palabras + índice de skills + AGENTS.md (`session_utils.py:32-39`) | Guía de uso de tools, git status/branch, reglas de seguridad y formato, fecha, entorno | Alto (F-25) |
| **Sub-agentes** | `agent`/`agents` paralelos con semáforo, telemetría live, fallback de modelo. Sin system prompt, sin permisos | Aislados, con tipos nombrados, mismo system prompt y misma política de permisos | Medio en features; **alto en seguridad** (F-01, F-23) |
| **Permisos** | allow/ask/deny por tool + patrones glob solo en `bash`; Shift+Tab cicla `bash` ask↔allow; fail-closed sin callback. Bypass fnmatch. No aplica en `-p` ni sub-agentes | Modos (plan / accept-edits / bypass), hooks PreToolUse/PostToolUse, diff preview antes de aprobar, parseo de comando | Medio en features; **alto en corrección** (F-02, F-03) |
| **Gestión de contexto** | Compactación con gate conservador, handoff estructurado de 7 secciones, offload a disco, aprendizaje de ventana desde 400s | Similar; compactación dirigida por el agente (H-9) | Bajo, salvo F-10/F-11 |
| **Retry / resiliencia** | Dos implementaciones, ninguna conectada | Backoff con jitter, streaming-aware | **Alto** (F-12) |
| **Headless / SDK** | `-p` texto plano, exit 0/1, sin middlewares. `AgentEngine` es API usable como librería | `--output-format json/stream-json`, `--resume/--continue`, SDK oficial, MCP OAuth | Alto |
| **Hooks de usuario** | Solo `AgentMiddleware` Python en plugins | Hooks shell/JSON en config (pre/post tool, stop, notification) | Medio |
| **Checkpoints / rewind** | Árbol de conversación con `/undo`, rewind picker, jump a nodo; `phoson_plugin_checkpoint` es storage Postgres de sesión | Snapshots de estado de archivos, worktrees aislados | Medio |
| **Evaluación** | `bench/` con 4 tareas triviales, sin baseline, sin CI | Suites internas tipo Terminal-Bench/SWE-bench con gate | Alto (H-1, ya trackeado) |
| **Observabilidad** | Métricas en header, `/cost`, `/tokens`, `/steps`; sin trazas | OTel con spans run→step→tool | Alto (H-2, ya trackeado) |
| **Proveedores** | 22 adaptadores; Anthropic/OpenAI/OpenRouter completos con caching; locales (vLLM/Ollama/LM Studio) de primera clase; Bedrock/Mistral/Gemini/Ollama incompletos | Claude Code: Anthropic (+Bedrock/Vertex). Codex: OpenAI. OpenCode/Aider: multi-proveedor | **Ventaja de phoson** en soberanía de proveedor; F-50 en la cola larga |
| **Skills / AGENTS.md / MCP** | Skills con progressive disclosure y compat `.claude/skills`; AGENTS.md jerárquico con `@import`; MCP stdio/sse/http con pooling ~11× y toggle granular | Paridad | Ninguno (F-04/F-08 son endurecimiento) |
| **Sesiones** | Árbol no lineal, JSONL atómico con fsync, Postgres opcional | Lineales con fork | **Ventaja de phoson** |
| **TUI** | prompt_toolkit + Rich; windowing O(visible); palette Ctrl+P; `!` shell; `@file`; imagen paste; temas 4 tiers + JSON; chip de modo; card de confirmación Yes/Always/No | Además: diff preview antes de aprobar edits, cola de mensajes mientras corre, Ctrl-R, vim mode, notificaciones, collapse por card | Bajo a medio; F-40/F-42 son bugs, no gaps |
| **Monitores / background** | `phoson_plugin_monitor` con wake persistente; sin background runs (I-129) | Background bash, tareas detachables | Medio (ya trackeado) |

**Lectura global.** Donde phoson invirtió (contexto, caching, sesiones, proveedores, MCP, skills, TUI) está a la par o por encima del SOTA. Donde no invirtió es exactamente la capa que la literatura de harness engineering señala como la palanca más barata con modelo fijo: la interfaz agente-computadora (edit tool, búsqueda, prompt de tools) y la coherencia de la frontera de seguridad. Antigravity coloca a phoson en "Nivel 2: Reactive Harness" con la fitness function como único paso a Nivel 3; esta revisión sostiene que **sin arreglar la ACI (F-20/F-21/F-25) el gate de H-1 mediría un harness con un edit tool que edita el sitio equivocado**, y que ese arreglo es previo y no depende del gate.

---

## 5. Detalle por capa

### 5.1 `phoson_agent` — loop, middleware, compactación

**Arquitectura (confirmada):** `AgentEngine._stream_impl` (`agent.py:323-407`) construye el onion de middlewares y corre `max_iterations`; cada iteración `AgentLoop.run_iteration` (`_loop.py:127-205`) demultiplexa el stream y devuelve `LLMStepOutcome`; sin tool calls → `AgentDoneEvent`; con tool calls → un mensaje assistant con todos los `ToolUseBlock` y luego `ToolRunner.execute` secuencial, un mensaje `user` con un `ToolResultBlock` por llamada. Permisos como middleware `on_before_tool`. Compactación en `wrap_llm_call` del summarizer. Cancelación por `asyncio` con backfill.

**Bien hecho:** separación engine/loop/runner con eventos sentinela tipados; backfill de cancelación con flag `committed`; refusals accionables como tool result; gate de compactación que cuenta schemas + system + reserva `max_tokens` + 10 % de margen y aprende la ventana real del 400; rescate de emergencia que encoge el propio prompt de resumen hasta que cabe; offload best-effort; throttle leading-edge de `ToolCallDeltaEvent`; breakpoints de cache en el adaptador Anthropic excluyendo `tool_use` correctamente.

**Bugs:** F-10, F-11, F-12, F-13, F-14, F-15, F-16, F-17, F-19. Ver §2.2.

**Gaps ya trackeados:** doom loops (H-3), budget en contexto (H-4), effort por fase (H-5), compact tool (H-9), tool budget (H-11), preserved thinking (I-134).

### 5.2 `phoson_llm` — adaptadores

**Bien hecho:** contrato `LLMEvent` con orden canónico; `ToolCallAccumulator` tolerante a JSON inválido; 3 breakpoints Anthropic; sticky routing OpenRouter por `session_id`; `RetryingChat` con semántica streaming-aware correcta.

**Bugs/gaps:** F-12 (retry no conectado), F-13 (`stop_reason`), F-50 (adaptadores incompletos), I-134.

### 5.3 `phoson_cli/tools` — interfaz agente-computadora

**Inventario:** `read_file`, `write_file`, `patch_file`, `list_dir`, `view_image`, `bash` (timeout por invocación, cap 50 KB UTF-8-safe, kill al expirar, `safe_mode` fail-closed), `skill` (solo si hay skills), `web_search` (DDG scraping / Brave / Tavily), `web_fetch` (cap 50 KB, 5 redirects), `agent`, `agents`. Todo con permiso `allow` por defecto.

**Bien hecho:** `_timeouts.py` con sanitización y nota autocorrectiva al modelo; `view_image` tipado; skills con truncado de cuerpo y compat `.claude/skills`; AGENTS.md jerárquico con `@import`; fan-out de `agents` con métricas live por tarea y fallback de modelo; disciplina de cache (fecha sin hora, lista de tools estable).

**Bugs/gaps:** F-01, F-04, F-05, F-06, F-08, F-20…F-26. Ver §2.1 y §2.3.

**Mejor y peor descripción de tool:** la mejor es `skill` (`skill.py:51-57`: cuándo llamarla, qué vuelve, qué hacer después). La peor es `patch_file` (`files.py:130`): "Replace `old_content` with `new_content` in a file." sin exactitud, whitespace, first-match ni preferencia sobre `write_file`.

### 5.4 `phoson_cli` — controller, comandos, config, sesiones

**Arquitectura (confirmada):** `__main__.py` parsea argv a mano → plugin subcommands / `--setup` / one-shot / REPL clásico o TUI. Ambos front-ends envuelven `SessionController` (UI-free) que emite a un `AgentEventSink`. `_rebuild_engine` es el único punto de creación de chat+engine+middlewares. `run_turn` serializa bajo `_turn_lock` y persiste con `JsonlStorage` tras cada estado terminal. `commands.py` con tabla de specs + comandos de plugin validados por rebuild.

**Bien hecho:** `chmod 0600/0700` en config y permissions; cierre de plugins al rebuild (I-P0); one-shot con exit codes correctos; `find_latest_node_id` determinista; updater con comandos fijos (no arbitrarios) y confirmación; plugin install con confirmación salvo `-y`.

**Bugs/gaps:** F-02, F-30…F-38. Features SOTA ausentes: JSON/stream-json, `--resume/--continue`, hooks de usuario, modos de permiso, comandos slash desde archivos, config por proyecto compartible, MCP OAuth, checkpoints de archivos, OTel.

### 5.5 `phoson_cli/fullscreen` — TUI

**Arquitectura (confirmada):** `run_turn` → `FullScreenSink.on_event` (bloques append-only + `CurrentTurn` mutable, `dirty` + `invalidate`) → `PhosonApp._render_chat` como callback de `FormattedTextControl` → `render_chat_split` con `BlockAnsiCache` (un render Rich por bloque y ancho) → slice a ventana visible → `ANSI(...)`. Throttle 10 fps en tokens, `min_redraw_interval=0.035`, ticker 0.12 s congelado durante streaming. Scroll lógico propio con cursor fijo en `Point(0,0)` y `ChatScrollbarMargin`. Keys table-driven remapeables; floats para confirmaciones, pickers y palette.

**Diff sin commitear (`_compute_chat_bounds` incremental + tests):** la lógica de splice es correcta en todos los casos revisados (prefijo vacío, tail vacío, prefijo terminado en `\n`, estado vacío); los tests nuevos comparan contra el scan completo. Import de `_line_boundaries` sigue en uso. **No corrige F-40**, que es previo.

**Bugs/gaps:** F-40…F-45. UX ausente vs SOTA: diff preview antes de aprobar `patch_file`/`write_file` (solo `bash` tiene card), cola de mensajes mientras corre (`submit` avisa y no encola, `app.py:1068`), Ctrl-R, vim mode, notificaciones, collapse por card (solo `/details` global), "[Pasted N lines]".

### 5.6 Plugins y bench

**Bien hecho:** MCP pooling con `AsyncExitStack` y locks por servidor, auto-reconexión, 3 transportes, toggle granular; Postgres transaccional; monitor con "disk is truth" y process groups; `bench/run_bench.py` con workspaces aislados y checkers deterministas.

**Bugs/gaps:** F-51, F-52; MCP no consume `readOnlyHint`/`destructiveHint` (H-6 fase 2); bench con 4 tareas y sin CI (H-1).

---

## 6. Plan de ataque unificado

Se respeta el orden de `ISSUES-COMPLEXITY.md` (Fases 4-8) y se inserta **una fase de corrección** antes, con la regla explícita de que **los bugs no esperan al gate**. Cada PR es pequeño, testeable sin LLM, y no mezcla concerns.

```
Fase 3.5 — Corrección y seguridad (no requieren H-1; todos con test de regresión)

  PR-A  TUI, en la rama actual antes de mergear #171
        F-40 ✅ ya en 40c8022 (epoch en la clave del slice)
        F-41 generación en fingerprint (BlockAnsiCache.clear bumpea contador)
        F-44 ajustar docstring/CHANGELOG "O(visible)" → "sin loop Python sobre el prefijo"
        F-42 strip de control codes en cuerpos de bash                          S

  PR-B  Fronteras de seguridad
        F-01 sub-agentes heredan PermissionMiddleware + safe_mode + confirmation
             (o fallan cerrado si no hay callback)
        F-02 one-shot construye la misma cadena Offload → Summarizer → Permission
             (H-7 wall-clock encaja aquí como tercer commit)
        F-03 match de allow-patterns sobre shlex.split(cmd)[0] + separadores
             (;, &&, ||, |) → rechazar o pedir confirmación
        F-07 match_args obligatorio; sin fallback a "primer string"             S-M

  PR-C  Edit tool seguro
        F-20 patch_file: error si count > 1 con replace_all=False, listando
             las líneas de cada match; opcional read-before-edit por sesión
        F-21a read_file con números de línea (formato cat -n) + cap en rangos
        F-25 descripciones con cuándo/cuándo-no + system prompt con guía de
             tools (preferir patch_file, ancla única, bash para grep) y
             git branch/status
        F-22 caps en list_dir y salida de agent(s)                              S-M

  PR-D  Robustez del loop
        F-10 cortar la compactación en fronteras seguras: nunca dejar un
             user/tool_result sin su assistant/tool_use en la cola
        F-11 resumen vacío → abortar compactación (no devolver compacted);
             pasar tools=[] al call_next del resumen
        F-12 conectar RetryingChat en build_chat; borrar o arreglar
             RetryMiddleware (visible = Token/Reasoning/ToolCall, no LLMStart)
        F-13 leer stop_reason; max_tokens → error accionable al modelo
        F-14 except Exception en ToolRunner con backfill de tool_result         M

  PR-E  Grep + Glob nativos (esto SÍ es hipótesis de harness → medir con H-1,
        pero la evidencia de Claude Code/SWE-agent justifica shippearlo ya)
        F-21b tools grep (rg si existe, fallback Python) y glob con .gitignore   M

  PR-F  Pequeños, independientes
        F-04 @import confinado al árbol del repo + expanduser
        F-06 web_fetch: bloquear IPs privadas/link-local, límite de descarga,
             aviso de contenido no confiable en el resultado
        F-23 sub-agentes con system prompt (cwd, fecha, AGENTS.md)
        F-24 quitar "agents" de los tools del hijo
        F-34 mapping de métricas en /resume
        F-35 persistir tras /compact
        F-36 _resolve_bool/_resolve_int
        F-37 expanduser + chmod en mcps.json
        F-38 timeout en run_upgrade_command                                     S

Fase 4 en adelante — sin cambios respecto a ISSUES-COMPLEXITY.md
  #140 slice 1 → #139 (H-1 con 15-25 tareas; PR-C/PR-E son los primeros
  cambios que ese gate debería medir) → #134 → #145 → #142/#143 → #144 → …

Deuda, cuando toque
  F-45 extraer ChatPane / HeaderModel / floats / RewindController de app.py;
       unificar CommandHost
  F-50 Bedrock streaming, Mistral tools, Gemini index, Ollama <think>
  F-51 TTL/cuota en compacted/
  F-52 Postgres append-only
  F-18 gather de tools read-only
```

**Por qué esta fase va antes de H-1 y no después.** El argumento de `ISSUES-COMPLEXITY.md` (regla 5) es que sin gate no se sabe si un cambio de harness mejora o empeora. Es correcto para cambios cuyo efecto depende del comportamiento del modelo. No lo es para un `tool_result` huérfano que produce un 400, un middleware de permisos que no está adjunto, o una ventana que no repinta. Esos se miden con tests unitarios, y ya hay 1937. Además, H-1 medirá un harness; conviene que sea uno cuyo edit tool no edite el sitio equivocado.

---

## 7. Límites de esta revisión

- **Verificación:** los ítems ✅ fueron leídos en código por esta sesión. Los ⚠️ los reportó un subagente con referencia archivo:línea y no se re-leyeron línea a línea; el riesgo es de detalle, no de existencia.
- **F-40** fue reproducido por el subagente con scripts de sondeo sobre `PhosonApp` (no en una sesión de terminal real). El código leído en `app.py:1017-1023` es consistente con la reproducción.
- **F-42** verifica el comportamiento de Rich (`Text(...).plain` conserva ESC) y de ptk `ANSI()` con OSC, no una captura de sesión real.
- **F-50** se verificó por grep, no leyendo los adaptadores completos.
- **No se ejecutó la suite de tests** (solo `--co`), ni el bench, ni pyright.
- **No se evaluó** `phoson_plugin_memory` ni `phoson_plugin_monitor` en profundidad; se toma la descripción de Antigravity y del ROADMAP.
- **La comparación SOTA** se basa en el conocimiento de los harness de referencia a la fecha del modelo; no se ejecutaron esas herramientas en paralelo para este documento.
- **Concurrencia:** `phoson_cli/fullscreen/app.py` fue editado durante la revisión (apareció `_compute_chat_bounds` y el fix de F-40, luego commiteados como `40c8022` y publicados en PR #173). Los números de línea del TUI corresponden al estado final leído.
- **Higiene del árbol:** al inicio de la sesión había tres archivos `deep_research_*.md` (856 líneas, investigación sobre LLMs de 1 bit) sin trackear en la raíz; al cerrar la revisión ya no estaban en el árbol de trabajo. Se menciona solo para que no vuelvan a la raíz del repo.
---

## 8. Relación con otros documentos

- `REVISION-BY-ANTIGRAVITY.md` — revisión previa sobre código; §3.1 de este documento lista qué confirma y qué corrige.
- `reporte-harness.md` — revisión externa sobre documentación; origen de H-1…H-11. §3.2 corrige la afirmación sobre fail-closed en no-interactivo.
- `IMPROVEMENTS.md` — board activo de H-*/I-*. Los F-* de §2 con issue lo tienen anotado en la columna Estado (#174–#189, abiertos el 2026-09-01); la tabla unificada está en `ROADMAP.md` §2.
- `IMPROVEMENTS-TUI.md` — look (T-*), todo shipped; F-40…F-45 son corrección/deuda de TUI, no look.
- `ISSUES-COMPLEXITY.md` — orden transversal; §6 propone insertar la Fase 3.5 entre su Fase 3 y su Fase 4.
