# IMPROVEMENTS — phoson-engine-minimal / phoson-cli

> **Origen:** investigación profunda del repo (agosto 2026): arquitectura del engine, comparativa contra el SOTA de agent harnesses (Claude Code, Codex, Pi, OpenCode, DeepSeek `dsh`, Qwen Code, Aider, mini-SWE-agent…) y auditoría de UI/UX de la capa CLI.
>
> **Cómo usar este documento:** cada ítem tiene ID, prioridad (P0–P3), esfuerzo estimado (S/M/L), impacto, y criterio de listo. La prioridad se calculó con: **(impacto en adopción × riesgo si no se hace) ÷ esfuerzo**. Los ítems P0 son los que bloquean uso serio hoy; P1 dan el mayor salto competitivo; P2 pulen; P3 son apuestas a futuro.
>
> **Estado de referencia:** v0.12.5 · 1254 tests passing · pyright 0 errors · ruff clean.
> **Progreso:** B1–B3 cerrados en v0.8.1 (PR #71) · A1 fase 1 (PR #75), A3 (PR #74) y A2/A4 (PR #76) cerrados en v0.9.0 · C1–C4 cerrados en v0.10.0 (PR #81) · D1–D5 cerrados en v0.11.0 · reasoning effort xhigh/max + contexto vLLM cerrados en v0.12.0 (PR #86) · E1 (context management avanzado) cerrado en v0.12.1 (PR #87) · E2 (subagent panel con métricas en vivo) cerrado en v0.12.2 (PR #90) · E3 (autocomplete de rutas y `@file` mentions) cerrado en v0.12.3 · E4 (themes interactivos y auto-detección light/dark) cerrado en v0.12.4 · E5 (check de updates al arrancar) cerrado en v0.12.5 · E6 (keybindings personalizables) cerrado en v0.12.6 · G1 (double-Esc rewind) cerrado en v0.13.0.

---

## Tabla de decisión rápida

| ID | Mejora | Prioridad | Esfuerzo | Impacto | Issue | ¿Cuándo? |
|----|--------|-----------|----------|---------|-------|----------|
| [B1](#b1-timezone-hardcodeado-en-el-system-prompt) | Timezone hardcodeado (`America/Mexico_City`) | **P0** | S | 🔴 Alto | — | ✅ v0.8.1 |
| [B2](#b2-lista-de-tools-hardcodeada-en-el-system-prompt) | Lista de tools hardcodeada en system prompt | **P0** | S | 🔴 Alto | — | ✅ v0.8.1 |
| [B3](#b3-borrado-destructivo-sin-confirmación) | Borrado de sesiones sin confirmación | **P0** | S | 🟠 Medio | — | ✅ v0.8.1 |
| [A1](#a1-permisos-por-herramienta) | Permisos por herramienta (fase 1) | **P0** | L | 🔴 Crítico | — | ✅ PR #75; sandbox diferido |
| [A2](#a2-input-multilinea--historial-persistente-en-la-tui) | Input multilinea + historial persistente (TUI) | **P0** | M | 🔴 Alto | — | ✅ v0.9.0 (PR #76) |
| [A3](#a3-agentsmd--memoria-persistente-via-filesystem) | `AGENTS.md` / memoria via filesystem | **P0** | M | 🔴 Crítico | — | ✅ PR #74 |
| [A4](#a4-feedback-de-enter-durante-un-run) | Feedback al presionar Enter durante un run | **P0** | S | 🟡 Bajo-Medio | — | ✅ v0.9.0 (PR #76) |
| [C1](#c1-panel-de-herramientas-en-vivo-pr-2-del-todo) | Panel de herramientas en vivo (diffs, labels) | **P1** | M | 🔴 Alto | — | ✅ Sprint 2 |
| [C2](#c2-comandos-p1-faltantes) | `/compact`, `/status`, `/resume <id>` | **P1** | M | 🔴 Alto | — | ✅ Sprint 2 |
| [C3](#c3-web-tools-web_search--web_fetch) | Web tools (`web_search`, `web_fetch`) | **P1** | M | 🔴 Alto | — | ✅ Sprint 2 |
| [C4](#c4-status-bar-persistente-y-look-feel-final-pr-3) | Status bar persistente + look & feel (PR-3) | **P1** | S-M | 🟠 Medio | — | ✅ Sprint 2 |
| [D1](#d1-limpieza-de-debt-arquitectónica) | Limpieza de debt: textual/, duplicados, REPL huérfano | **P2** | M | 🟡 Bajo-Medio | — | ✅ Sprint 3 |
| [D2](#d2-consolidar-el-repl-clásico-o-darle-salida) | Consolidar o retirar el REPL clásico | **P2** | S-M | 🟠 Medio | — | ✅ Sprint 3 |
| [D3](#d3-corregir-ctrlv-y-soporte-macos-clipboard) | Ctrl+V en macOS + conflicto con paste de texto | **P2** | S | 🟠 Medio | — | ✅ Sprint 3 |
| [D4](#d4-tests-e2e-visuales-de-la-tui) | Tests e2e/visuales de la TUI | **P2** | M-L | 🟠 Medio | — | ✅ Sprint 3 |
| [D5](#d5-flags-cli-faltantes) | Flags CLI: `--version`, `--model`, `--provider`, `--classic` | **P2** | S | 🟠 Medio | — | ✅ Sprint 3 |
| [E1](#e1-context-management-avanzado-retained-reasoning--compaction-con-control) | Context management avanzado (retained reasoning) | **P3** | L | 🔴 Alto | — | ✅ v0.12.1 (PR #87) |
| [E2](#e2-panel-de-subagentes-con-métricas-en-vivo) | Subagent panel con métricas en vivo | **P3** | M | 🟠 Medio | — | ✅ v0.12.2 (PR #90) |
| [E3](#e3-autocompletado-de-rutas-y-file-mentions) | Autocomplete de rutas y `@file` mentions | **P3** | M | 🟠 Medio | — | ✅ v0.12.3 |
| [E4](#e4-themes-interactivos-y-auto-detección-lightdark) | `/theme` interactivo + auto-detección light/dark | **P3** | S | 🟢 Bajo | — | ✅ v0.12.4 |
| [E5](#e5-check-de-updates-al-arrancar) | Check de updates al arrancar | **P3** | S | 🟢 Bajo | — | ✅ v0.12.5 |
| [E6](#e6-keybindings-personalizables) | Keybindings configurables | **P3** | M | 🟢 Bajo | — | ✅ v0.12.6 |
| [G1](https://github.com/phoson-lat/phoson-engine-minimal/issues/51) | Double-Esc para retroceder a un mensaje anterior (rewind) | **P1** | M | 🟠 Medio | [#51](https://github.com/phoson-lat/phoson-engine-minimal/issues/51) | ✅ v0.13.0 |
| [G2](https://github.com/phoson-lat/phoson-engine-minimal/issues/69) | Prompt caching (OpenRouter/Anthropic) — tokens cacheados + cabeceras | **P1** | M | 🔴 Alto | [#69](https://github.com/phoson-lat/phoson-engine-minimal/issues/69) | Sprint 2–3 |
| [G3](https://github.com/phoson-lat/phoson-engine-minimal/issues/57) | Seleccionar/copiar texto del chat con el mouse (full-screen) | **P2** | S-M | 🟠 Medio | [#57](https://github.com/phoson-lat/phoson-engine-minimal/issues/57) | Sprint 3 |
| [G4](https://github.com/phoson-lat/phoson-engine-minimal/issues/58) | Hyperlinks clicables en respuestas (OSC 8 vía prompt_toolkit) | **P2** | M | 🟠 Medio | [#58](https://github.com/phoson-lat/phoson-engine-minimal/issues/58) | Sprint 3+ |
| [G5](https://github.com/phoson-lat/phoson-engine-minimal/issues/52) | Sistema de Skills (instrucciones on-demand, tipo Claude Code) | **P3** | L | 🟠 Medio | [#52](https://github.com/phoson-lat/phoson-engine-minimal/issues/52) | Backlog (diseño) |

> **Fila `G*`** — mejoras que ya existían como *issues* abiertos de GitHub (no surgieron de la auditoría) y que se añaden aquí para tener un único tablero. El ID `G*` es local a esta tabla; la columna **Issue** enlaza al issue real.

### G1 — Double-Esc rewind ([#51](https://github.com/phoson-lat/phoson-engine-minimal/issues/51)) ✅ v0.13.0
`Esc` dos veces en idle abre un picker con los mensajes *user* de la ruta activa; al elegir uno el cursor aterriza en el nodo **anterior** (mismo contrato que `/undo`: lo "deshecho" queda como rama abandonada en el árbol, visible en `/tree`; métricas de costo/tokens acumulativas, no se roll back) y la TUI **redibuja el pane desde el árbol** hasta ese punto (`_reset_transcript` + `print_history`, el mismo mecanismo que `/resume`), rellena el composer con el texto del mensaje (editar + Enter re-envía) y apila el punto previo en `PhosonApp._rewind_stack` (los rewinds consecutivos se apilan). **Deshacer el salto: `Ctrl+Z`** (`undo_jump`, remapeable en `[keys]`; el buffer del composer ignora Ctrl+Z) — Esc no podía usarse: contendría con el Esc-único que cancela el run en vuelo (#68). Precedencia: el `escape` sigue siendo *eager* (un solo Esc en vuelo cancela de inmediato, sin ventana de doble-tap) y el doble-tap se detecta en el app con una ventana de 1.0 s sobre el monotonic clock (un chord nativo `"escape escape"` jamás dispararía porque el binding eager consume cada `escape`; la ventana es deliberadamente mayor que `ttimeoutlen` de prompt_toolkit, 0.5 s: la capa VT100 retiene la entrega de un Esc *solo* ese tiempo para desambiguarlo del inicio de una secuencia de escape, de modo que la brecha *entregada* entre dos Escs en idle queda acotada a ~0.5 s y una ventana de 0.5 s fallaría doble-taps reales). Por eso el doble-tap *viaja* con la tecla `escape`: remapear `escape` mueve cancelación y rewind juntos, y `escape = ""` desactiva ambos. Nuevo picker `rewind_picker.py` (patrón `BasePicker`, paged) y primitivas en el controller (`jump_candidates` / `jump_to_user_turn` / `jump_to_node`, generalización de `undo_last_turn`; la raíz se excluye — no hay punto previo). Footer actualizado; `/keys` lista `undo_jump`. Tests: `test_g1_rewind_unit.py` (26) + e2e de teclas reales.

### G2 — Prompt caching ([#69](https://github.com/phoson-lat/phoson-engine-minimal/issues/69))
El engine reenvía el prompt completo en cada request. Añadir soporte de prompt caching (OpenRouter/Anthropic): cabeceras/parámetros de caché, y exponer métricas de tokens cacheados (`cache_read_input_tokens` / `cache_creation_input_tokens`). Beneficio: −50–90% de costo en prefijos repetitivos y menos latencia. Requiere mantener prefijos estables (system prompt + tools al inicio — ya lo hace B2 al derivar la lista).

### G3 — Seleccionar/copiar texto con el mouse ([#57](https://github.com/phoson-lat/phoson-engine-minimal/issues/57))
En full-screen, `mouse_support=True` (para la rueda del chat) captura el mouse y quita la selección nativa del terminal. Fijar: documentar el workaround de *Shift+drag* si funciona (fix más barato), y/o un modo de copia por teclado (seleccionar rango con flechas → yank al clipboard vía el mismo `xclip`/`wl-copy` que usa Ctrl+V).

### G4 — Hyperlinks clicables ([#58](https://github.com/phoson-lat/phoson-engine-minimal/issues/58))
`formatting.py` pasa `hyperlinks=False` a `Markdown(...)` porque el parser `ANSI()` de prompt_toolkit no entiende OSC 8 y mostraba los bytes crudos. Ahora los links salen como `text (url)` inerte. El fix real necesita un spike: si el renderer de prompt_toolkit puede emitir bytes de escape crudos intactos (reinyectar OSC 8 post-procesando el ANSI) o renderizar los links como fragmento custom.

### G5 — Sistema de Skills ([#52](https://github.com/phoson-lat/phoson-engine-minimal/issues/52))
Abstracción distinta de plugins y tools: un paquete descubrible de instrucciones (y scripts/recursos) que se carga en contexto **solo cuando es relevante**. Requiere diseño antes de implementar: qué dispara la carga (slash/keyword/tool call), dónde viven (`.phoson/skills/` vs `~/.phoson/skills/`), y cómo interactúa con `build_tools()`. Placeholder de diseño, no spec lista.

---

## Leyenda

- **Prioridad** — P0 = bloqueante para uso serio; P1 = máximo salto competitivo; P2 = pulido y deuda; P3 = backlog/apuestas.
- **Esfuerzo** — S = <1 día; M = 1–3 días; L = >3 días (o requiere diseño).
- **Impacto** — 🔴 crítico · 🟠 medio · 🟡 bajo-medio · 🟢 bajo.

---

# P0 — Bloqueantes

Estos impiden usar phoson-cli como herramienta de trabajo diaria o lo exponen a errores visibles. Atacar primero.

---

### B1 — Timezone hardcodeado en el system prompt
**Archivo:** `phoson_cli/session_utils.py:44` · **Esfuerzo:** S · **Impacto:** 🔴 · **Estado:** ✅ **Hecho en v0.8.1** (PR #71)

**Problema.** El system prompt inyecta la hora usando `ZoneInfo("America/Mexico_City")` fijo. Cualquier usuario fuera de CDMX recibe una hora incorrecta en el contexto del agente — y como el modelo la usa para razonar ("¿qué día es hoy?", logs, cron), corrompe silenciosamente su comportamiento.

**Fix propuesto.**
```python
from datetime import datetime
from zoneinfo import ZoneInfo
import tzlocal  # o: time.tzname / datetime.now().astimezone().tzinfo

tz = ZoneInfo(tzlocal.get_localzone())   # zona real del sistema
now = datetime.now(tz)
```
Sin dependencia nueva si se usa `datetime.now().astimezone()` (respeta `TZ` del sistema). Fallback a UTC si falla.

**Criterio de listo.** Test que setea `TZ=Europe/Madrid` y verifica que el prompt contiene esa zona. Un solo commit.

---

### B2 — Lista de tools hardcodeada en el system prompt
**Archivo:** `phoson_cli/session_utils.py::build_system_prompt` · **Esfuerzo:** S · **Impacto:** 🔴 · **Estado:** ✅ **Hecho en v0.8.1** (PR #71)

**Problema.** El prompt dice literalmente `"read_file, write_file, patch_file, list_dir, bash, web_search, agent, agents"`. Si el registro real cambia (añades/quitas una tool, cargas MCP servers), el prompt miente al modelo → intenta llamar tools inexistentes u omite las nuevas. El TODO P0 ya arregló un caso idéntico ("stale system prompt"); este es otro superviviente.

**Fix propuesto.** Derivar la lista de `self.engine.tools` (ya disponible en el controller):
```python
tool_names = ", ".join(sorted(t.name for t in tools))
```
Y añadir un test de regresión que construya el prompt con un registro modificado y verifique que refleja los nombres reales.

**Criterio de listo.** El prompt siempre coincide con `build_tools_dict().keys()`. Un test lo garantiza.

---

### B3 — Borrado destructivo sin confirmación
**Archivos:** `phoson_cli/fullscreen/command_host.py::pick_session`, `/delete` handler · **Esfuerzo:** S · **Impacto:** 🟠 · **Estado:** ✅ **Hecho en v0.8.1** (PR #71)

**Problema.** En el picker de sesiones, `d` borra una sesión y `X` borra todas las marcadas sin ningún y/N. Lo mismo `/delete <id>`. Una pulsación accidental pierde una conversación (posiblemente larga y costosa en tokens) permanentemente. Ninguna otra herramienta comparable (Claude Code `/resume`, OpenCode) borra sin confirmar.

**Fix propuesto.** Reusar `run_float_confirm` (ya existe y está testeado):
```python
if await self.app.run_float_confirm(f"Delete {len(ids)} session(s)? This cannot be undone."):
    ...
```

**Criterio de listo.** Test del host: borrar con confirmación cancelada no elimina nada; confirmada sí. Footer del picker muestra "space mark · X delete (asks)".

---

### A1 — Permisos por herramienta
**Área:** seguridad · **Esfuerzo:** L · **Impacto:** 🔴 crítico · **Estado:** ✅ **Fase 1 hecha** (PR #75)

La fase 1 reemplaza el anterior enfoque `safe_mode` all-or-nothing con permisos declarativos por herramienta, patrones de allowlist y rechazo seguro en contextos no interactivos. El sandbox OS-level se ha diferido conscientemente al final del backlog: aporta aislamiento real, pero introduce complejidad de plataforma, dependencias y semántica de filesystem/red que no debe bloquear las mejoras activas.

**Fase 1 — Modelo de permisos (sin sandbox), completada:**

Config declarativa en `permissions.json`:
```toml
[permissions]
bash = "ask"          # "allow" | "ask" | "deny"
bash.allow_patterns = ["git *", "pytest*", "uv *"]   # glob patterns
edit_files = "allow"
web_search = "deny"
```
   - Motor: un `PermissionMiddleware` en `phoson_agent` que intercepte `on_before_tool` y consulte una tabla `{tool_name: level}` con matching por patrón.
   - UI: extender el float de confirmación a 3 opciones — `[y] once  [a] always for this pattern  [n] no` — persistiendo "always" en config.toml.
   - Runtime: comando `/permissions` para listar/cambiar niveles en caliente.
   - Tests: matriz de combinaciones tool×nivel×patrón.

**Criterio de listo alcanzado.** Un comando no permitido se bloquea con mensaje accionable; las allowlists persisten entre sesiones; el middleware y el store tienen cobertura de regresión.

**Nota de diseño.** La capa de permisos es *middleware* (no lógica hardcodeada en las tools), lo que mantiene la filosofía plugin-first y permite reutilización por Phoson-Core. El aislamiento OS-level queda en el backlog final de este documento.

---

### A2 — Input multilinea + historial persistente en la TUI
**Archivo:** `phoson_cli/fullscreen/app.py::_build_layout` · **Esfuerzo:** M · **Impacto:** 🔴 · **Estado:** ✅ **Hecho** (PR #76; follow-up de layout en v0.9.1)

**Problema doble:**
1. `TextArea(height=1, multiline=False)` — no hay forma de escribir prompts largos ni pegar bloques de código multi-línea. El TUI Textual descartado sí tenía Shift+Enter; la TUI actual perdió esa capacidad en la migración.
2. `InMemoryHistory()` — el historial de inputs muere al cerrar; el REPL clásico sí persiste en `~/.phoson/history.txt`. Inconsistencia injustificable entre frontends.

**Propuesta.**
- `TextArea(height=DYNAMIC)` con altura dinámica (crece hasta N líneas, luego scroll interno). prompt_toolkit soporta `height=DYNAMIC` con `char="..."`.
- Bindings: `Enter` envía (comportamiento actual intacto); `Ctrl+J` inserta newline. Se eligió `Ctrl+J` porque Shift/Ctrl/Alt+Enter no se mapean de forma portable en el parser VT100 de prompt_toolkit. El footer documenta el binding.
- Historial: reemplazar `InMemoryHistory` por `FileHistory("~/.phoson/history.txt")` (mismo archivo que el clásico → historial compartido). `↑/↓` navega entradas previas cuando el cursor está en la primera línea (prompt_toolkit ya hace esto si no compite con el scroll).
- Opcional (fase 2 del ítem): binding `Ctrl+E` para abrir el input actual en `$EDITOR` (patrón de Claude Code/Aider para prompts largos).

**Criterio de listo.**
- `Ctrl+J` inserta newline y Enter envía; test de bindings cubre ambos. ✅
- Al reiniciar la TUI, `↑` recupera el último mensaje de la sesión anterior. ✅ (test `test_up_arrow_recalls_previous_session_history` + `test_history_survives_an_app_restart`)
- El footer refleja el hint correcto según estado. ✅

**Nota de integración (follow-up).** El primer merge (PR #76) dejó dos bugs de layout en el composer:
1. `wrap_lines=False` — las líneas largas no se envolvían y se perdían por el borde derecho al escribir/pegar código. Corregido a `wrap_lines=True` (el default de `TextArea`).
2. `D(min=1, max=5)` sin `dont_extend_height` — el pase "fill to max" de `HSplit` inflaba el input vacío hasta su altura máxima: el prompt ocupaba **5 líneas** en vez de 1. Corregido con `dont_extend_height=True` (el composer toma exactamente la altura de su contenido, con tope de 5); el pane de chat absorbe el resto. Tests de regresión: `test_input_window_reports_content_height_not_max` y la aserción de `wrap_lines()` en `test_input_is_multiline_with_dynamic_height`.

---

### A3 — `AGENTS.md` / memoria persistente vía filesystem
**Área:** context management · **Esfuerzo:** M · **Impacto:** 🔴 crítico · **Estado:** ✅ **Hecho** (PR #74)

**Por qué es P0.** Es **el patrón dominante del SOTA** (Claude Code `CLAUDE.md`, Codex/Gemini/Pi/OpenCode/Qwen `AGENTS.md`) y phoson no tiene equivalente. La memoria actual existe solo vía plugins (Redis/Postgres/Qdrant), que requieren infra y no sirven para el caso de uso más común: "recuerda que en este repo usamos ruff y pytest, nunca black". El filesystem es gratis, versionable con git y visible para el usuario.

**Propuesta (alineada con la jerarquía estándar emergente):**
1. Carga al construir el system prompt (orden de precedencia, concatenando):
   - `~/.phoson/AGENTS.md` (global usuario)
   - `AGENTS.md` en cada directorio desde la raíz del repo hasta cwd (jerárquico)
   - Soportar también `CLAUDE.md` como alias si existe (compatibilidad con repos ya configurados para otras herramientas).
2. Sintaxis mínima: markdown plano + `@ruta/archivo.md` para imports explícitos (como Gemini CLI).
3. Indicador en la TUI: el header muestra `📄 agents.md` cuando hay archivos cargados; comando `/agents-md` lista qué archivos se inyectaron y sus tamaños.
4. Presupuesto: cap configurable (`agents_md_max_tokens`, default ~2000) con truncamiento avisado — evita que un AGENTS.md gigante coma el contexto.
5. Cache-busting: re-leer los archivos en cada turno (son chicos; evita stale state tras editarlos).

**Criterio de listo.**
- Test: crear `AGENTS.md` temporal con contenido distintivo, verificar que aparece en el system prompt enviado al LLM (mock del chat).
- Jerarquía correcta: archivo en subdirectorio sobrescribe/complementa al de la raíz según orden definido y documentado.
- Docs: sección nueva en README con ejemplo de AGENTS.md.

---

### A4 — Feedback al presionar Enter durante un run
**Archivo:** `phoson_cli/fullscreen/app.py::submit` · **Esfuerzo:** S · **Impacto:** 🟡→🟠 (barato pero mejora la percepción) · **Estado:** ✅ **Hecho** (PR #76)

**Problema.** `_is_run_in_flight()` descarta el envío en silencio: el usuario escribe, da Enter, no pasa nada, y no sabe si el programa colgó, ignoró el texto o hay que reintentar.

**Propuesta.** Cuando esté en vuelo:
- Notificar con `sink.notify("warn", "A turn is already running — Esc to cancel it first.")`, y
- Guardar el texto en el buffer (no limpiarlo), para que el usuario no pierda lo escrito.

Extra barato: mostrar en el header el estado ya existente (`status_text()` devuelve "Streaming"/"Running tool") con un spinner animado junto al status para que sea obvio sin leer.

**Criterio de listo.** Test: submit durante run → buffer conserva texto y sink recibió el warn. ✅ (`test_submit_while_run_in_flight_keeps_text_and_warns`; el estado en vivo del header queda cubierto por `test_header_shows_live_status_while_a_run_is_in_flight`).

**Nota (follow-up v0.9.1).** El feedback visual vive en el **área del chat** (no en el header): al enviar aparece de inmediato un spinner transitorio con frases rotativas de *thinking* (`Thinking…`, `Pondering the problem…`, … — lista editable en `_THINKING_PHRASES` de `sink.py`, una frase nueva cada ~2.5 s), incluso antes del primer evento del provider; el label cambia fijo a `Streaming…`, `Running tool…` o `Running subagents…` según la fase, y toda la línea desaparece al completar o cancelar el turno. Está cubierto por pruebas de inicio inmediato, rotación de frases, fases, limpieza y cancelación pre-provider.

---

# P1 — Máximo salto competitivo

Después de P0, estas son las mejoras que más acercan phoson-cli a la experiencia de las herramientas líderes.

---

### C1 — Panel de herramientas en vivo (PR-2 del TODO)
**Esfuerzo:** M · **Impacto:** 🔴 · **Estado:** ✅ **Hecho en v0.10.0** (PR #81)

**Qué falta.** Las llamadas a herramientas se ven como una línea `⚙ label · preview[:50]…` con spinner. Sin diffs coloreados al editar, sin "writing file (path, +N lines)", sin feedback de progreso. Claude Code y OpenCode muestran tarjetas ricas por herramienta; es de lo primero que nota un usuario nuevo.

**Propuesta (según el propio plan PR-2 del TODO):**
1. **Labels accionables por tool**: mapping `tool_name → verbo humano` ("writing file", "running command", "searching", "spawning subagent").
2. **Línea de detalle**: path relativo + tamaño estimado para files; comando truncado para bash; patrón para search.
3. **Resultados especializados**:
   - `edit_file`/`patch_file` → diff coloreado (verde/rojo, estilo unified, truncado a ±20 líneas con aviso de truncado). Rich ya trae `Syntax` y se puede renderizar un diff manual con `Text` estilizado.
   - `write_file` → `✓ created src/x.py · 42 lines · 1.8 KB`.
   - `bash` → primeras N líneas de stdout/stderr + exit code.
4. **Spinner con frases rotatorias** mientras corre (ya existe infraestructura en `WaitingSpinner`; añadir rotación cada ~2.5s).
5. **Tiempo transcurrido en vivo** en el card (no solo `duration_ms` al terminar).

**Criterio de listo.** Editar un archivo muestra diff coloreado legible; escribir uno muestra líneas/KB; el card de bash muestra salida inicial. Tests de los formatters puros nuevos (siguiendo el patrón `formatting.py`).

**Nota de arquitectura.** Todo el formato debe vivir en `formatting.py` como renderables puros para que ambos frontends lo reutilicen (patrón ya establecido).

**Follow-up v0.10.0.** En el frontend full-screen, la card final reemplaza in-place su línea de inicio mediante `tool_call_id` (también para calls paralelas), evitando headers duplicados en el transcript.

---

### C2 — Comandos P1 faltantes
**Esfuerzo:** M · **Impacto:** 🔴 · **Estado:** ✅ **Hecho en v0.10.0** (PR #81)

Tres comandos que el propio TODO ya identifica como P1 y que completan el control del runtime:

1. **`/compact`** — disparador manual de la compactación. Hoy `SummarizationMiddleware` auto-corre a 80% de la ventana sin control del usuario. Implementación:
   - Con argumento opcional (`/compact keep last 10`) a futuro; v1: fuerza resumen ahora y muestra tokens antes→después.
   - Requiere exponer un método del summarizer (`force_compact(path) -> (before, after)`) y un evento de feedback en el sink.
2. **`/status`** — vista única que reemplace los cuatro atomizados (`/env`, `/cost`, `/tokens`, `/steps`): provider · model · cwd · sesión · costo acumulado · tokens usados/ventana · pasos · MCP servers activos · permisos activos. El estado de sandbox se añadirá solo después de implementar la fase 2 diferida de A1. Renderizable como tabla Rich compacta. Mantener los comandos viejos como aliases (no romper scripts).
3. **`/resume <id>`** — carga directa por id (hoy solo vía picker). Trivial sobre `load_session` existente; autocomplete con `SessionsArgCompleter` ya preparado para esto.

**Criterio de listo.** `/compact` reduce los tokens estimados y muestra el delta; `/status` imprime todas las dimensiones listadas; `/resume <id-parcial>` carga la sesión correcta (match por prefijo).

---

### C3 — Web tools (`web_search`, `web_fetch`)
**Esfuerzo:** M · **Impacto:** 🔴 · **Estado:** ✅ **Hecho en v0.10.0** (PR #81)

**Qué falta.** El sistema de tools del CLI es austero: `read_file/write_file/patch_file/list_dir/bash/search/subagent(s)/view_image`. No hay acceso a la web — capacidad estándar en todos los harnesses comparables (Claude Code, Codex, Qwen, OpenCode). Para un agente de coding esto significa: no puede mirar docs de una librería, verificar un issue, o buscar el error exacto de un stacktrace.

**Propuesta.**
1. **`web_fetch(url) -> str`** — httpx (dependencia ya presente) + lectura a markdown/texto plano con límite de tamaño (~50KB) y timeout. Strip de HTML con `selectolax` o regex ligero (evitar BeautifulSoup pesado; mantener "minimal").
2. **`web_search(query) -> str`** — backend configurable (TODO P3 ya lo apunta):
   - Default: DuckDuckGo HTML scraping (gratis, frágil — aceptable como default con disclaimer).
   - Configurable a Brave/Serper/Tavily con API key en config.toml/env.
3. Ambas como tools del registro estándar (`@tool`), con permisos integrados al modelo de A1 (`web_search = "ask"` por defecto, p.ej.).
4. Sanitización: truncar output, escapar contenido antes de inyectarlo en el contexto (superficie de prompt-injection — documentar).

**Criterio de listo.** El agente responde correctamente a "busca el changelog de rich y dime qué cambió en la última versión" usando las dos tools encadenadas. Tests unitarios con respuestas mock.

---

### C4 — Status bar persistente y look & feel final (PR-3)
**Esfuerzo:** S-M · **Impacto:** 🟠 · **Estado:** ✅ **Hecho en v0.10.0** (PR #81)

Completar el PR-3 pendiente del TODO — el último tercio del plan look & feel:

1. **Header consolidado + footer de shortcuts**: `model (provider) · cwd · tokens used/window · $cost sesión` vive en una sola barra superior fija junto con marca, indicadores transitorios y estado de run; el footer inferior conserva solo los shortcuts de teclado. Se evitó deliberadamente repetir provider/model/tokens/costo en ambas barras. MCP y permisos siguen disponibles en `/status`; el estado de sandbox se añadirá solo después de A1 fase 2.
2. **`/tree` coloreado** — árbol ASCII actual es monocromo; colorear nodo actual, ramas abandonadas en muted, labels en accent.
3. **`/help` agrupado por categorías** (Session / Model / Info / Config / System) — 21 specs en lista plana hoy.
4. **Error hints**: los paneles de error ganan una línea "hint" cuando el código es conocido (`auth` → "run /setup"; `max_iterations` → "raise with /config max_iterations"; `rate_limit` → "wait or switch model").
5. **Banner**: animación sutil opcional (frames ASCII del logo) — low priority, detrás de un flag.

**Criterio de listo.** El footer siempre refleja el estado real (probar cambiando modelo/provider/permisos; el sandbox se añadirá tras la fase 2 diferida de A1); `/help` agrupa visualmente; errores comunes muestran hint accionable.

---

# P2 — Pulido y deuda técnica

No bloquean, pero su costo crece si se deja pasar y afectan la mantenibilidad justo cuando el equipo va a acelerar.

---

### D1 — Limpieza de deuda arquitectónica
**Esfuerzo:** M · **Impacto:** 🟡 · **Estado:** ✅ **Hecho** (Sprint 3)

Checklist concreto encontrado en la auditoría:

- [x] **Eliminar `phoson_cli/textual/`** — directorio vacío con solo `__pycache__` (residuo de v0.7.0). Un `git rm -r`.
- [x] **Unificar `_PROVIDER_LABELS`** — duplicado en `provider_picker.py` e `installer.py`. Extraer a `models.py` o un módulo compartido. → `phoson_cli/labels.py` (`PROVIDER_LABELS` + `provider_label()`); la tabla del picker (con aliases `grok`/`google`/`aws`) se convierte en la única fuente y el wizard pasa a leerla.
- [x] **Unificar `_SPINNER_FRAMES`** — duplicado en `renderer.py` y `subagent_panel.py`. Extraer a un módulo `animations.py`. → `phoson_cli/animations.py` (`SPINNER_FRAMES` + `spinner_frame()`); también se unifica la copia `_ACTIVITY_SPINNER_FRAMES` del sink full-screen.
- [x] **Unificar `_token_indicator()`** — implementación idéntica en `repl.py` y `fullscreen/app.py`. Mover a `formatting.py` como función pura `format_token_indicator(used, window)`.
- [x] **Unificar slash completers** — `_SlashCompleter` (repl.py) vs `SlashCompleter` (fullscreen/completer.py). El del fullscreen es más capaz; migrar y borrar el otro. → `SlashCompleter` vive ahora en `commands.py` (junto a `COMMAND_SPECS`/`COMMANDS`, de donde toma sus datos); ambos frontends lo importan y `fullscreen/completer.py` lo re-exporta por compatibilidad.
- [x] **Eliminar `branch_session`** — deprecated no-op en DOS capas (repl + controller). Rompe API pública menor; hacerlo en la próxima minor con entrada en CHANGELOG. → eliminada en v0.11.0 (entrada en CHANGELOG).
- [x] **Actualizar `TODO.md`** — fecha de cabecera (2026-08-20) desactualizada, LOC de repl.py incorrecto ("579", hoy 487). Este documento (`IMPROVEMENTS.md`) puede absorber/reemplazar parte del TODO. → cabecera 2026-08-25, secciones de estado actualizadas, LOC corregido; IMPROVEMENTS.md queda como tablero activo.

**Criterio de listo.** `grep -rn "_PROVIDER_LABELS\|_SPINNER_FRAMES\|_token_indicator\|branch_session" phoson_cli/` devuelve una sola definición por símbolo (o cero para branch_session).

---

### D2 — Consolidar el REPL clásico o darle salida
**Esfuerzo:** S-M · **Impacto:** 🟠 · **Estado:** ✅ **Hecho** (Opción A, Sprint 3)

**Problema.** `__main__.py` lanza siempre `PhosonApp`; `repl.py` (487 LOC) + `renderer.py` (635 LOC) + `ClassicSink` solo son alcanzables desde tests. Son ~1.100 LOC de frontend duplicado cuya lógica **ya diverge** (el clásico imprime `render_start_line`, el fullscreen no; spinners en threads vs ticker async).

**Decisión: Opción A — dar salida al clásico como modo degradado.**

- `phoson-cli --classic` (o `--no-fullscreen`) lanza el REPL clásico end-to-end.
- Auto-detección: si `TERM` está vacío o es `dumb` en un terminal interactivo, se elige el clásico automáticamente con aviso en stderr (solo aplica a TTYs reales; stdin piped es one-shot y no lo dispara).
- Estatus documentado en el docstring de `repl.py`: frontend *retained degraded mode*, segundo frontend sobre el mismo controller ("a sink, not a fork"), y hogar de las primitivas de render clásico (`Renderer`, `ClassicSink`) que ambos frontends comparten donde es posible.
- Las diferencias de comportamiento (p.ej. `render_start_line` en el clásico) quedan como decisiones conscientes y documentadas: el clásico es append-only (scrollback), el fullscreen es un transcript re-renderizable.

**Criterio de listo.** `phoson-cli --classic` arranca el REPL clásico funcionando end-to-end. ✅ (tests de selección de frontend y de `main()` en `test_cli_args_unit.py`).

---

### D3 — Corregir Ctrl+V y soporte macOS clipboard
**Archivos:** `fullscreen/clipboard.py`, `fullscreen/app.py` · **Esfuerzo:** S · **Impacto:** 🟠 · **Estado:** ✅ **Hecho** (Sprint 3)

**Dos problemas:**
1. **Sin macOS**: `read_clipboard_image()` solo probaba `wl-paste` (Wayland) y `xclip` (X11). En Mac, Ctrl+V de imagen fallaba silenciosamente ("No image on the clipboard"). Se añade soporte de `pngpaste` (vía `brew install pngpaste`), `pbpaste` para texto, y un hint explicativo en el mensaje de error cuando se ejecuta en macOS sin la herramienta instalada (`macos_image_tool_hint()`).
2. **Conflicto con paste de texto**: Ctrl+V estaba rebind-eado globalmente a "pegar imagen", tragándose el paste de texto nativo de `TextArea`. Comportamiento resuelto: se intenta leer la imagen primero; si el clipboard no contiene imagen, se lee el texto plano mediante la herramienta del SO (`wl-paste --no-newline`, `xclip -o`, `pbpaste`) y se inserta en el buffer en la posición del cursor (`_paste_text_fallback()`), sin perder nunca el paste de texto.

**Criterio de listo.** En Linux: Ctrl+V con imagen en clipboard pega placeholder `[image #N]`; con texto en clipboard pega el texto. En macOS: soporte `pngpaste`/`pbpaste` con mensaje claro `brew install pngpaste` si no está instalado. Tests unitarios del backend macOS, hint, fallback a texto y routing en `test_clipboard_unit.py` y `test_fullscreen_shell_unit.py`.

---

### D4 — Tests e2e/visuales de la TUI
**Esfuerzo:** M-L · **Impacto:** 🟠 · **Estado:** ✅ **Hecho** (Sprint 3)

**Qué faltaba.** Cobertura de la `Application` real: routing de teclas, ciclo de vida headless, y render visual.

**Implementado (`tests/phoson_cli/fullscreen/test_e2e_tui.py`):**
1. **Routing real de bindings con `create_pipe_input`**:
   - `Ctrl+J` / `\n`: inserción de nueva línea en el composer multilínea sin enviar.
   - `Ctrl+L` / `\x0c`: limpieza de transcript completa.
   - `Ctrl+C` / `\x03`: con un turno activo cancela el turno sin salir de la app; el segundo `Ctrl+C` sale limpiamente.
   - `Escape` / `\x1b`: cancelación del turno en vuelo.
2. **Ciclo headless e2e**: arranque de `PhosonApp`, envío de mensaje por `submit()`, streaming de tokens y finalización con `AgentDoneEvent` contra chat mock, verificando el transcript renderizado final.
3. **Golden ANSI snapshots (4 estados clave)**:
   - Transcript vacío (`Type a message and press Enter.`).
   - Turno en streaming activo (badge de usuario, prompt, label "Phoson" y texto parcial).
   - Panel de error con hint accionable (`hint: run /setup`).
   - Card de herramienta finalizada (`read_file`, path, duración en ms).

**Criterio de listo.** Los 5 bindings críticos tienen test de routing real; 4 golden snapshots en CI. ✅ (`test_e2e_tui.py`).

---

### D5 — Flags CLI faltantes
**Archivo:** `__main__.py` · **Esfuerzo:** S · **Impacto:** 🟠 · **Estado:** ✅ **Hecho** (Sprint 3)

Faltan flags básicos que cualquier CLI moderno expone (y que facilitan scripting/CI):

```
--version                  # print importlib.metadata version y sale (hoy no existe)
--model <id>               # override puntual sin tocar config.toml
--provider <id>            # ídem
--theme <tier>             # override puntual
--classic                  # REPL clásico (ver D2)
--no-fullscreen            # alias de --classic
--max-turns <n>            # override de max_iterations para esta corrida
--dry-run                  # (opcional, post-A1) muestra qué tools ejecutaría sin permiso
```

Mantener el parsing manual (typer/click añadiría dependencia contraria a la filosofía minimal) pero centralizarlo en una función pura `parse_args(argv) -> CliOptions` testeable, hoy parcialmente inline.

**Implementado:** parsing manual centralizado en `parse_args(argv) -> CliOptions` (dataclass), testeable sin proceso. Flags: `--version`, `--model`, `--provider`, `--theme`, `--max-turns`, `--classic`, `--no-fullscreen`, `-p/--print`, `--setup`, `--self-update`, `--uninstall`, `-h/--help`. Overrides aplicados sobre el config cargado (flag > config.toml > env > default) y re-aplicados tras el reload del wizard. Errores de parsing salen con código 2 (comportamiento argparse-compatible). `--dry-run` diferido: requiere la fase 2 de A1 (sandbox) para tener semántica útil.

**Criterio de listo.** Cada flag con test unitario del parsing y efecto verificado; `--version` imprime y sale 0. ✅ (`test_cli_args_unit.py`).

---

# P3 — Backlog / apuestas a futuro

Valor alto pero requieren diseño o dependen de P0-P1 madurar primero.

### A1 fase 2 — Sandbox opt-in para bash (diferido al final del backlog)

**Área:** seguridad · **Esfuerzo:** L · **Impacto:** 🔴 crítico · **Estado:** diferido conscientemente

La fase 1 de permisos ya está integrada; el sandbox OS-level se ejecutará **después de los demás ítems activos de este documento**, no en Sprint 3. Requiere una decisión de diseño separada para no mezclar permisos de herramientas con aislamiento de procesos ni duplicar el `safe_mode` heredado.

- Linux: `bubblewrap` (`bwrap`) o Landlock; macOS: Seatbelt (`sandbox-exec`); fallback sin sandbox con warning visible.
- Modo `workspace-write`: escritura únicamente en el cwd y red denegada por defecto, con flag explícito para habilitarla.
- Detección de backend al arrancar y una interfaz `/sandbox on|off|status`.
- Criterio de listo: con `PHOSON_SANDBOX=bwrap`, `touch /etc/passwd` falla, `ls` funciona y la documentación explica cómo instalar `bwrap`.

La fase de contenedores docker/podman sigue siendo una extensión post-P1 de este trabajo diferido; no se programa hasta que el sandbox opt-in tenga una interfaz estable.


---

### E1 — Context management avanzado (retained reasoning + compaction con control)
**Esfuerzo:** L · **Impacto:** 🔴 (para tareas largas) · **Estado:** ✅ **Hecho en v0.12.1** (PR #87)

El summarizer actual (auto a 80%) era funcional pero primitivo frente al SOTA. Implementado:

- **Retained reasoning** (Codex): al compactar, el razonamiento capturado de los turnos del segmento (persistido en `node.metadata["reasoning"]`) se pliega en el prompt del resumen, de modo que el documento de handoff conserva el *por qué* de las decisiones clave, no solo sus conclusiones. OpenAI reporta que esta sola técnica mejora la continuidad en tareas largas (13.3%→38.3% en ARC-AGI-3). El controller registra el reasoning del run activo antes de lanzarlo y lo limpia al terminar (cualquier estado terminal).
- **Compaction estructurada**: el resumen es un documento con secciones fijas (Goal / Completed / Key decisions / Reasoning highlights / Open questions / Next steps / Constraints and context — patrón Anthropic long-running agents), consumible de forma fiable por el siguiente segmento. Auto y manual comparten un único prompt builder (`SummarizationMiddleware.build_summary_prompt`), así que producen siempre el mismo artefacto.
- **Offload de tool outputs grandes**: `OffloadMiddleware` nuevo (`phoson_agent/plugins/offload.py`, middleware por principio #2, no lógica en tools). Resultados >`offload_max_chars` (default 24 KB) van a `~/.phoson/compacted/` y el contexto solo ve head/tail + path; el modelo recupera el contenido con `read_file`. Best-effort (un fallo de escritura nunca rompe el run) y conmutable con `offload_tool_outputs`.
- **Control fino**: `compact_mode = "balanced" | "aggressive" | "off"` con presets (aggressive compaction al 65% de la ventana y cola más corta; off desactiva el auto-compact — el manual sigue funcionando), más knobs explícitos `compact_threshold` / `compact_min_keep_messages` (env `PHOSON_*` o `[defaults]` de config.toml; el valor explícito gana sobre el preset).
- **`/compact` con preview + confirmación**: `/compact` muestra qué haría ("summarize N of M turns, keeping K, ~T tokens") y pregunta antes de pagar la llamada de resumen; `/compact aggressive` idem con corte más profundo; `/compact yes` aplica el último preview sin preguntar; `/compact on|off` conmuta el auto-compact en caliente (persiste en config.toml).

**Criterio de listo.** Todo cubierto con tests: `tests/phoson_agent/test_offload.py`, `tests/phoson_agent/test_summarizer_e1.py`, `tests/phoson_cli/test_e1_context_unit.py`.

**Dependía de:** C2 (`/compact`) como base de UI — ya cerrado en v0.10.0.

---

### E2 — Panel de subagentes con métricas en vivo
**Esfuerzo:** M · **Impacto:** 🟠 · **Estado:** ✅ **Hecho en v0.12.2** (PR #90)

Hoy el panel en vivo muestra solo spinners ("waiting"); tiempo/tokens/costo aparecen recién en el summary final (el wire format `--- METRICS: ...` llega al terminar). Propuesta: streaming incremental del bloque METRICS (emitir línea partial cada N segundos desde el subagent) para alimentar las columnas Time/Tokens/Cost en vivo. Requiere tocar el protocolo producer-side en `tools/subagent.py` y el parser (que ya es tolerante a parciales — ventaja).

**Implementado.** En vez de streamear líneas parciales de `--- METRICS` (que el parser ya tolera, pero que duplicaría el wire format), el progreso fluye por la misma vía que el resto del estado del run: los tools `agent`/`agents` son *tools de primera clase* del engine y reciben el progreso del inner run a través de los `AgentStepDoneEvent` que el engine ya emite por cada LLM call completado.

- **Producer** (`tools/subagent.py`): `SubagentProgressTracker` por *llamada de tool* (un run puede llamar a `agent`/`agents` varias veces; cada una ve solo sus propias filas, sin drift de índices). `_stream_final` acepta un callback `on_event` que pliega cada `RunStep` LLM del subagent en el tracker *mientras corre* (tokens/cost de la llamada ya terminada, igual que el `AgentStepDoneEvent` del parent). Al terminar, `finalize` hace el snap final con los mismos números del summary (sum de `step.duration_ms`, tokens/cost de `AgentRunResult`), así el panel en vivo y el summary final son consistentes. Timeout/error/cancel → `mark_error` (fila ✗, sin crash).
- **Transporte**: el tracker se empuja a la UI vía callback `on_subagent_progress` inyectado en el `AgentContext.extra` (el controller lo enlaza a `sink.on_subagent_progress`). Los tools son agnósticos de UI: sin callback (one-shot, scripts, tests) todo funciona igual. La notificación ocurre *dentro* del run, cuando `AgentStartEvent` ya creó el `CurrentTurn` — no hay race con el placeholder pre-provider.
- **Consumer fullscreen** (`fullscreen/sink.py`): `CurrentTurn.subagent_progress` + `on_subagent_progress`; el ticker existente (`_SUBAGENT_TICK_SECONDS`) ya invalida el paint, y el panel ahora renderiza Time (wall-clock en vivo para tareas corriendo, congelado al finalizar) / Tokens / Cost por tarea. Tareas en cola de la semáfora de paralelismo siguen mostrando "waiting" en Time hasta que arrancan de verdad.
- **Consumer clásico** (`renderer.py`): `SubagentSpinner.set_progress()` — el hilo de animación relee el tracker cada frame (12 fps), mismo efecto que el fullscreen.
- **Wire format final intacto**: `format_metrics_line`/`parse_subagent_metrics`/`render_subagent_summary` no cambian; el summary al terminar sigue siendo la fuente de verdad para el transcript.

**Criterio de listo.** Tests en `tests/phoson_cli/test_subagent_live_metrics.py`: tracker (register/start/update/finalize/mark_error, solo LLM steps, snap final, elapsed bounds), tools alimentando el tracker *en vivo* (valores intermedios visibles a mitad de run + snap final + error/timeout + tracker independiente por llamada + callback notificado/limpiado), panel (live values, queued "waiting", done ✓ / error ✗, fallback sin tracker, tracker vs lista equivalente), controller (callback inyectado en `context.extra`) y fullscreen sink (panel renderiza del tracker y vuelve a "waiting" al limpiarse; tracker no se pierde aunque llegue antes que `AgentToolStartEvent`).

---

### E3 — Autocompletado de rutas y `@file` mentions
**Esfuerzo:** M · **Impacto:** 🟠 · **Estado:** ✅ **Hecho en v0.12.3**

Patrón estándar (Cursor, Amp, Claude Code @-mentions): escribir `@src/` despliega archivos del repo filtrables fuzzy; el mention se expande a contenido adjunto (o referencia contextual). Implementable con un `PathCompleter` custom sobre el merge_completers existente + expansión en `controller._build_user_message`. Complementa A2 (multilinea) para flujos de "arregla el bug en @src/foo.py".

**Implementado.** El work se dividió en un núcleo UI-independent + un completer compartido + expansión en el controller (misma capa que ambos front ends):

- **Núcleo** (`phoson_cli/file_mentions.py`, nuevo): `expand_file_mentions(text, cwd)` parsea tokens `@mention` (regex con lookbehind para ignorar `user@domain` y handles sueltos), los resuelve (relativo → cwd, `~/` → home, absoluto → tal cual) y construye el bloque de contenido: **texto inline** (con cap head/tail a 32 KB, misma idea que el offload de tool outputs) para código/datos, y **bloques media nativos** (image/audio/video/pdf, los mismos que `/attach` construye) para binarios. Guards: máx. 10 mentions por mensaje (el resto queda como texto + aviso) y máx. 20 MB por archivo (igual que `view_image`). Un `@path` que no resuelve se deja como texto y se reporta al usuario; un token suelto sin `/` (p. ej. `@team` o un email) se deja en silencio — es prosa, no un archivo. `iter_candidate_paths` camina el árbol con caps (profundidad 6, 2000 entradas) y salta `.git`/`node_modules`/… para que un repo grande nunca bloquee el input.
- **Completer** (`commands.py` → `PathCompleter`, compartido, re-exportado en `fullscreen/completer.py`): ofrece rutas del repo cuando el buffer termina en `@` (inicio o tras whitespace), fuzzy-filtrado con el mismo `FuzzyCompleter`/`WordCompleter` que `/model`. Walk lazy (una vez, al primer `@`), con hint de tamaño por archivo (`14 B` / `2.0 KB` / `1.2 MB`). Al estar en `commands.py` (no `fullscreen/`), el REPL clásico puede usarlo sin ciclo de imports `repl → fullscreen → app → repl`.
- **Expansión** (`controller._build_user_message`): el mensaje que recibe el modelo conserva el texto raw (el `@mention` sigue visible en el contexto) + los bloques resueltos, en orden. Feedback vía sink: una línea `Attached: src/foo.py (14 B)` (info) + un aviso por referencia rota; cero ruido para texto sin mentions. Funciona igual para fullscreen, clásico y tests; one-shot no se toca.
- **Front ends**: `PhosonApp` añade `PathCompleter(Path.cwd())` al `merge_completers` existente; `PhosonRepl.run` hace `merge_completers([SlashCompleter(), PathCompleter()])`.

**Criterio de listo.** Tests en `tests/phoson_cli/test_e3_file_mentions_unit.py` (42): walk bounded (files+dirs, árboles ignorados, caps de profundidad/entradas), parseo+resolución (inline de texto, bloques media, missing/bare/email, dedupe, tilde/absoluto, caps de tamaño/cantidad, cwd string), `PathCompleter` (ofrecer/filtrar/start-position/mid-sentence/hint de tamaño/walk lazy/negativos), y wiring del controller (inline, attach + mention combinado, notify al adjuntar, warn al faltar, silencio en bare handle, texto plano sin cambios).

---

### E4 — Themes interactivos y auto-detección light/dark
**Esfuerzo:** S · **Impacto:** 🟢 · **Estado:** ✅ **Hecho en v0.12.4**

- Auto-detección: consultar `COLORFGBG` / query OSC 11 al terminal para sugerir light vs dark la primera vez (confirmar y persistir). Muchos terminales modernos lo reportan.
- `/theme` picker con preview en vivo del banner y una muestra de cada token (aprovechar BasePicker).

**Implementado.** Dos piezas independientes: una capa de detección UI-independent y un picker compartido reutilizable por los dos front ends.

- **Detección** (`phoson_cli/terminal_theme.py`, nuevo): `detect_terminal_theme()` → `True` (light) / `False` (dark) / `None` (no clasificable). Orden: env `COLORFGBG` (forma `fg;bg` con índices 16-color o palabras `light`/`dark` de tmux) y, si no hay, una query **OSC 11** al terminal (`\x1b]11;?\x07`, respuesta con color sRGB). El probe es best-effort y nunca lanza: pone el TTY en raw mode solo durante el probe (canonical mode se tragaría la respuesta, que no lleva newline), timeout ~150 ms, y `termios`/`tty` opcionales (no POSIX → `None`). Clasificación por luminancia relativa WCAG con umbral 0.5. IO inyectable (`tty_fd`/`write`/`read`) para tests sin TTY.
- **Sugerencia** (`theme.suggest_theme` + `__main__._maybe_offer_theme_suggestion`): solo si el usuario nunca fijó theme (ni `PHOSON_THEME` ni `theme` en config.toml, vía `config.has_persisted_theme`) y no pasó `--theme` este run. Si la detección resuelve, pregunta una línea `[Y/n]` y persiste con `save_config(only_fields={"theme"})`; si no resuelve (`None`) o no-color está activo, no pregunta. Corre **antes** de construir el front end, así el theme confirmado ya tiñe el banner del arranque. Dispara a lo sumo una vez (queda persistido).
- **Picker** (`phoson_cli/theme_picker.py`, nuevo): `BasePicker[ThemePickerResult]` idéntico en estructura a model/provider/session. Una fila por tier (`dark`/`light`/`ansi`/`no-color`) + **preview en vivo** del tier seleccionado: el banner (art + wordmark) y una tira de swatches de cada token, ambos renderizados con *los colores del tier en preview* (Rich → ANSI → `to_formatted_text`) para que sea WYSIWYG aunque el chrome del frame conserve la paleta del theme activo. Marca `(current)` y `(detected)`. `build_theme_picker` + `pick_theme`.
- **Wiring**: `CommandHandler._cmd_theme` (spec `/theme` en `COMMAND_SPECS` + `HELP_CATEGORIES`) abre el picker vía `host.pick_theme` (o `list` / arg directo), resuelve con `theme.get_theme` (lookup directo, ignora overrides de env), persiste y llama `host.apply_theme`. Protocolo `CommandHost` gana `pick_theme` + `apply_theme`.
- **Aplicar en vivo** (sin reiniciar): `PhosonRepl.apply_theme` re- apunta `self.theme`, `renderer.theme` y el subagent spinner; el prompt clásico re-aplica `build_prompt_style` cada pase. `PhosonApp.apply_theme` extiende eso con los consumidores propios del TUI: `sink.theme`, re-render del banner in-place, `_apply_style` (style dict prompt_toolkit: chat pane, header, composer, float frames) y limpia el `BlockAnsiCache` para repintar el pane. `StaticArgCompleter("/theme ", …)` da autocomplete de los 4 tiers.

**Criterio de listo.** Tests en `tests/phoson_cli/test_e4_themes_unit.py` (86): parseo `COLORFGBG` (índices 16-color, tmux light/dark, garbage), parseo respuesta OSC 11 (rgb/#hex/decimal, terminador BEL/ST, ruido alrededor, negativas), `query_terminal_bg_light` (IO inyectado, no-TTY, OSError, sin respuesta), `detect_terminal_theme` (capas), `suggest_theme`, `get_theme`, `has_persisted_theme`, el picker (fila initial, `detected`, navegación, Enter/Esc, wrap, preview del tier correcto, escapes parseados, no-color, float plumbing), `/theme` (picker, cancel, arg explícito, unknown, list), `PhosonRepl.apply_theme`, `PhosonApp.apply_theme`, `FullScreenCommandHost.pick_theme`, first-run suggestion (flag, persistido, unknown terminal, accept/decline/EOF), wizard theme prompt (detección, default, fallback) y E2E a través de `PhosonApp` (arg explícito, picker confirm, picker cancel). Suite ahora 1224 passing, pyright 0 errors, ruff clean.

---

### E5 — Check de updates al arrancar
**Esfuerzo:** S · **Impacto:** 🟢 · **Estado:** ✅ **Hecho en v0.12.5**

La infraestructura existe (`updater.py`, check PyPI offline-safe). Añadir check asíncrono no-bloqueante al inicio (una vez cada 24h, cache en `~/.phoson/last_update_check`); si hay versión nueva, una línea discreta en el footer/header: "⬆ v0.8.1 available — /update". Nunca bloquear el paint.

**Implementado.** El work se dividió en un núcleo UI-independent en `updater.py` + wiring mínimo en los dos front ends (mismo principio de "un solo home" que E4):

- **Núcleo** (`phoson_cli/updater.py`): `check_for_startup_update()` — gateo por cadencia sobre un cache JSON (`~/.phoson/last_update_check`, sobrenombrable vía `PHOSON_HOME`) con `{checked_at, ok, latest_version}`. Due cuando el cache falta/corrompe, tiene ≥24 h, o el último *intento* no tuvo éxito (`ok: false`) — el fallo resetea el intervalo para reintentar offline en el siguiente start sin martillear PyPI; un éxito (incluido "no hay update") duerme el intervalo completo. Fetch reutiliza `get_latest_version` (timeout 10 s) y `is_update_available`; escribe el cache de forma atómica (tmp + `os.replace`) y best-effort (HOME read-only → solo no-check). `startup_check_due` es puro y testable (`now` inyectable). Devuelve la versión solo si es estrictamente más nueva; cualquier error degrada a `None` (sin banner, sin mensaje, sin retry loop).
- **Wiring REPL clásico** (`repl.py`): `start_update_check()` lanza el check como `create_task` desde `run()` (no bloquea el primer prompt ni el input), almacena el resultado en `self.update_hint`, y `shutdown()` cancela el task si sigue en vuelo. El hint se pinta como fragmento propio `class:prompt.update` al final de la línea de prompt (dim, igual que los tokens): `phoson [model·node·12.4k/128.0k] › ⬆ v0.8.1 available — /update`.
- **Wiring TUI** (`fullscreen/app.py`): `run_async()` arranca el check sobre el REPL compartido (fuente única de verdad en ambos front ends) con `on_settle=self.app.invalidate` para que la cabecera se repinte en cuanto el check aterriza, incluso en pantalla 100 % idle. El hint se renderiza como segmento `header_dim` al final del header compacto (la línea de abajo sigue siendo solo hints de teclado).
- **Temas** (`theme.py`): `prompt.update` reutiliza `theme.prompt_tokens` (dim, nunca bold/accent) — sin tokens de color nuevos.

**Criterio de listo.** Tests en `tests/phoson_cli/test_e5_update_check_unit.py` (30): path del cache (default + `PHOSON_HOME`), gateo por cadencia (missing, corrupto, shape erróneo, sin `checked_at`, stale, boundary exacto, fallo reciente → retry, éxito → 24 h), texto del hint, flujo completo (escribe cache con la versión, no-newer → `null` en cache, dev → siempre hint, offline → retry next start, not-due → cero fetch, timeout 10 s, escritura no-throw en HOME read-only), wiring clásico (fragmento con/sin hint, orden de fragmentos, `start_update_check` → hint, fallo → `None`, callback `on_settle`, cancel en `shutdown`), wiring TUI (header con/sin hint estilo `header_dim`, `run_async` arranca el check, offline → sin hint), y `prompt.update` en `build_prompt_style`. Suite ahora 1254 passing, pyright 0 errors, ruff clean.

---

### E6 — Keybindings personalizables
**Esfuerzo:** M · **Impacto:** 🟢 · **✅ Cerrado en v0.12.6**

Sección `[keys]` en config.toml mapeando acciones → teclas (`toggle_reasoning = "c-t"`). Construir los bindings desde esa tabla en `keys.py`. Baja prioridad: el set actual es razonable y el costo de soportar remapeos (docs, conflictos, validación) supera el beneficio hasta tener base de usuarios pidiéndolo.

**Implementado.** El mapa de teclas de la TUI pasó de hardcodeado a derivado de una tabla (`DEFAULT_KEY_BINDINGS`) con overrides del usuario:

- **Núcleo** (`fullscreen/keys.py`): `DEFAULT_KEY_BINDINGS` — `{acción: [secuencias]}` con la precedencia histórica (`line_up = ["s-up", "c-up"]`, `exit = ["c-q", "c-c"]`). `resolve_key_bindings(overrides)` mezcla defaults + overrides y **detecta conflictos cruzados** (una secuencia ligada a dos acciones → error, no un steal silencioso). `build_key_bindings(app, overrides)` construye el `KeyBindings` desde la tabla (chords tipo `"c-x c-e"`, `escape` mantiene `eager=True`); `listing_for_config` da las filas `(acción, "Ctrl+X")` para `/keys` (acción desligada → `(off)`).
- **Config** (`config.py`): `PhosonConfig.key_bindings: dict[str, list[str]] | None` + `load_key_bindings()` lee `[keys]` de `~/.phoson/config.toml` con validación dura — acción desconocida, tipo incorrecto, secuencia no parseable (vía `_parse_key` de prompt_toolkit) o lista vacía levantan `PhosonKeyBindingsError` (subclase de `PhosonConfigError`); `load_config` lo propaga y `main()` lo imprime y hace `exit(1)` con mensaje amigable (sin traceback). La sección `[keys]` es **user-managed** (como `permissions.json`): `save_config` nunca la escribe, así un valor stale no puede sombrear la tabla editada a mano. `KNOWN_KEY_ACTIONS` es el canonical de nombres; `""` desliga una acción; `"c-x c-e"` es un chord; listas = orden de precedencia.
- **Wiring TUI** (`fullscreen/app.py`): `PhosonApp` lee `config.key_bindings` al construir (un conflicto levanta en el constructor, antes del primer paint); `keys_listing()` expone el mapa efectivo.
- **Comando `/keys`** (`commands.py`): lista el mapa efectivo (defaults o remaps) en ambos front ends + sintaxis de `[keys]` y reglas de validación. Categoría "Config & System" en `/help`.
- **Docs:** README (sección "Key bindings (customizable)" + `/keys` en la lista de comandos), `docs/api/phoson_cli.md` (sección "Key bindings (customizable, full-screen TUI)" + fila en la tabla de comandos).

**Criterio de listo.** Tests en `tests/phoson_cli/test_e6_keybindings_unit.py` (43): shape del mapa por defecto (cubre `KNOWN_KEY_ACTIONS`, idéntico al set hardcodeado histórico, secuencias parseables), merge (copy sin mutation, override, unbind, unknown-action ignorado, conflictos cross-action, self-remap permitido, dos overrides en una secuencia), wiring TUI (defaults completos, `eager` de `escape`, remap mueve el binding, unbind desaparece, chord, conflicto → `PhosonKeyBindingsError` en el constructor, remap de `escape` conserva `eager`), display (`PgUp`/`Ctrl+T`/chords/`(off)`/orden), capa de config (no-file, sin sección, string/lista, chord, unbind, tabla vacía → defaults, unknown action, bad sequence, wrong type, entry no-string, lista vacía, integración `load_config`, round-trip `save_config` preserva `[keys]` user-managed, no es clave managed), `main()` falla amigable (SystemExit 1, sin traceback), y `/keys` (registro, categoría de help, output con remaps, dispatchable en ambos front ends). Suite ahora 1297 passing, pyright 0 errors, ruff clean.

---

## Roadmap sugerido (secuencia de ataque)

```
Sprint 0–1 (P0, cerrado)
├── B1/B2/B3 quick wins      ✅ v0.8.1
├── A1.fase1 permisos         ✅ PR #75
├── A3 AGENTS.md              ✅ PR #74
└── A2 multiline + history y A4 enter feedback  ✅ PR #76 (v0.9.0)

Sprint 2 (competitividad, ~1-2 semanas)
├── C1 tool cards ricos          ✅ sprint P1
├── C2 /compact /status /resume  ✅ sprint P1
├── C3 web tools                 ✅ sprint P1 (adelantado)
└── C4 status bar + help/tree    ✅ sprint P1

Sprint 3 (alcance, ~1-2 semanas)
├── D5 flags CLI
└── D1 limpieza deuda

Continuo / paralelo
├── D2 destino del REPL clásico (decisión, luego 1 día)
├── D3 Ctrl+V cross-platform
├── D4 tests e2e (ir acumulando con cada sprint)
├── E1 context management avanzado  ✅ v0.12.1 (PR #87)
├── E2 subagent panel métricas en vivo  ✅ v0.12.2 (PR #90)
├── E3 autocomplete de rutas + @file mentions  ✅ v0.12.3
├── E4 themes interactivos + auto-detección light/dark  ✅ v0.12.4
├── E5 check de updates al arrancar  ✅ v0.12.5
└── E6 según demanda real de usuarios  ✅ v0.12.6
```

## Principios para decidir durante la ejecución

1. **Todo formato nuevo va a `formatting.py` como renderable puro** — los dos frontends deben seguir compartiendo el 100%.
2. **Toda capability de seguridad es middleware/plugin**, no lógica en las tools — protege la filosofía framework-free y beneficia a Phoson-Core.
3. **Cada ítem termina con test + entrada en CHANGELOG** — la disciplina actual (1254 tests) es la mayor fortaleza del repo; no erosionarla por velocidad.
4. **Medir antes de optimizar** — los números de perf existentes (~18× cache, 16fps throttle) vinieron de medir; los nuevos features con componente de render deben incluir su medición.
5. **Cuando dude entre añadir feature o pulir existente**: el comparativo de harnesses mostró que *menos tools pero mejores* gana (caso Vercel: −80% tools → 100% éxito). Preferir profundidad sobre amplitud.

---

*Documento generado a partir de la auditoría completa del repo (agosto 2026). Actualizar checkboxes al cerrar cada ítem.*
