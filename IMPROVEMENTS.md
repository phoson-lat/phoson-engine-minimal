# IMPROVEMENTS — phoson-engine-minimal / phoson-cli

> **Origen:** investigación profunda del repo (agosto 2026): arquitectura del engine, comparativa contra el SOTA de agent harnesses (Claude Code, Codex, Pi, OpenCode, DeepSeek `dsh`, Qwen Code, Aider, mini-SWE-agent…) y auditoría de UI/UX de la capa CLI.
>
> **Cómo usar este documento:** cada ítem tiene ID, prioridad (P0–P3), esfuerzo estimado (S/M/L), impacto, y criterio de listo. La prioridad se calculó con: **(impacto en adopción × riesgo si no se hace) ÷ esfuerzo**. Los ítems P0 son los que bloquean uso serio hoy; P1 dan el mayor salto competitivo; P2 pulen; P3 son apuestas a futuro.
>
> **Estado de referencia:** v0.8.1 · 845 tests passing · pyright 0 errors · ruff clean.
> **Progreso:** B1, B2 y B3 cerrados en v0.8.1 (PR #71).

---

## Tabla de decisión rápida

| ID | Mejora | Prioridad | Esfuerzo | Impacto | Issue | ¿Cuándo? |
|----|--------|-----------|----------|---------|-------|----------|
| [B1](#b1-timezone-hardcodeado-en-el-system-prompt) | Timezone hardcodeado (`America/Mexico_City`) | **P0** | S | 🔴 Alto | — | ✅ v0.8.1 |
| [B2](#b2-lista-de-tools-hardcodeada-en-el-system-prompt) | Lista de tools hardcodeada en system prompt | **P0** | S | 🔴 Alto | — | ✅ v0.8.1 |
| [B3](#b3-borrado-destructivo-sin-confirmación) | Borrado de sesiones sin confirmación | **P0** | S | 🟠 Medio | — | ✅ v0.8.1 |
| [A1](#a1-permisos-por-herramienta) | Permisos por herramienta (fase 1) | **P0** | L | 🔴 Crítico | — | ✅ PR #75; sandbox diferido |
| [A2](#a2-input-multilinea--historial-persistente-en-la-tui) | Input multilinea + historial persistente (TUI) | **P0** | M | 🔴 Alto | — | En integración |
| [A3](#a3-agentsmd--memoria-persistente-via-filesystem) | `AGENTS.md` / memoria via filesystem | **P0** | M | 🔴 Crítico | — | ✅ PR #74 |
| [A4](#a4-feedback-de-enter-durante-un-run) | Feedback al presionar Enter durante un run | **P0** | S | 🟡 Bajo-Medio | — | En integración |
| [C1](#c1-panel-de-herramientas-en-vivo-pr-2-del-todo) | Panel de herramientas en vivo (diffs, labels) | **P1** | M | 🔴 Alto | — | Sprint 2 |
| [C2](#c2-comandos-p1-faltantes) | `/compact`, `/status`, `/resume <id>` | **P1** | M | 🔴 Alto | — | Sprint 2 |
| [C3](#c3-web-tools-web_search--web_fetch) | Web tools (`web_search`, `web_fetch`) | **P1** | M | 🔴 Alto | — | Sprint 2–3 |
| [C4](#c4-status-bar-persistente-y-look-feel-final-pr-3) | Status bar persistente + look & feel (PR-3) | **P1** | S-M | 🟠 Medio | — | Sprint 2 |
| [D1](#d1-limpieza-de-debt-arquitectónica) | Limpieza de debt: textual/, duplicados, REPL huérfano | **P2** | M | 🟡 Bajo-Medio | — | Sprint 3+ |
| [D2](#d2-consolidar-el-repl-clásico-o-darle-salida) | Consolidar o retirar el REPL clásico | **P2** | S-M | 🟠 Medio | — | Sprint 3 |
| [D3](#d3-corregir-ctrlv-y-soporte-macos-clipboard) | Ctrl+V en macOS + conflicto con paste de texto | **P2** | S | 🟠 Medio | — | Sprint 3 |
| [D4](#d4-tests-e2e-visuales-de-la-tui) | Tests e2e/visuales de la TUI | **P2** | M-L | 🟠 Medio | — | Sprint 3+ |
| [D5](#d5-flags-cli-faltantes) | Flags CLI: `--version`, `--model`, `--provider`, `--classic` | **P2** | S | 🟠 Medio | — | Sprint 3 |
| [E1](#e1-context-management-avanzado-retained-reasoning--compaction-con-control) | Context management avanzado (retained reasoning) | **P3** | L | 🔴 Alto | — | Post-P1 |
| [E2](#e2-panel-de-subagentes-con-métricas-en-vivo) | Subagent panel con métricas en vivo | **P3** | M | 🟠 Medio | — | Post-P1 |
| [E3](#e3-autocompletado-de-rutas-y-file-mentions) | Autocomplete de rutas y `@file` mentions | **P3** | M | 🟠 Medio | — | Post-P1 |
| [E4](#e4-themes-interactivos-y-auto-detección-lightdark) | `/theme` interactivo + auto-detección light/dark | **P3** | S | 🟢 Bajo | — | Backlog |
| [E5](#e5-check-de-updates-al-arrancar) | Check de updates al arrancar | **P3** | S | 🟢 Bajo | — | Backlog |
| [E6](#e6-keybindings-personalizables) | Keybindings configurables | **P3** | M | 🟢 Bajo | — | Backlog |
| [G1](https://github.com/phoson-lat/phoson-engine-minimal/issues/51) | Double-Esc para retroceder a un mensaje anterior (rewind) | **P1** | M | 🟠 Medio | [#51](https://github.com/phoson-lat/phoson-engine-minimal/issues/51) | Sprint 2–3 |
| [G2](https://github.com/phoson-lat/phoson-engine-minimal/issues/69) | Prompt caching (OpenRouter/Anthropic) — tokens cacheados + cabeceras | **P1** | M | 🔴 Alto | [#69](https://github.com/phoson-lat/phoson-engine-minimal/issues/69) | Sprint 2–3 |
| [G3](https://github.com/phoson-lat/phoson-engine-minimal/issues/57) | Seleccionar/copiar texto del chat con el mouse (full-screen) | **P2** | S-M | 🟠 Medio | [#57](https://github.com/phoson-lat/phoson-engine-minimal/issues/57) | Sprint 3 |
| [G4](https://github.com/phoson-lat/phoson-engine-minimal/issues/58) | Hyperlinks clicables en respuestas (OSC 8 vía prompt_toolkit) | **P2** | M | 🟠 Medio | [#58](https://github.com/phoson-lat/phoson-engine-minimal/issues/58) | Sprint 3+ |
| [G5](https://github.com/phoson-lat/phoson-engine-minimal/issues/52) | Sistema de Skills (instrucciones on-demand, tipo Claude Code) | **P3** | L | 🟠 Medio | [#52](https://github.com/phoson-lat/phoson-engine-minimal/issues/52) | Backlog (diseño) |

> **Fila `G*`** — mejoras que ya existían como *issues* abiertos de GitHub (no surgieron de la auditoría) y que se añaden aquí para tener un único tablero. El ID `G*` es local a esta tabla; la columna **Issue** enlaza al issue real.

### G1 — Double-Esc rewind ([#51](https://github.com/phoson-lat/phoson-engine-minimal/issues/51))
Presionar `Esc` dos veces (en idle) debería retroceder a un mensaje anterior y descartar lo que viene después (UX de Claude Code). El árbol ya soporta saltar a cualquier nodo (`current_node_id` es settable); el trabajo es la detección del double-tap y dejar elegir *a qué* turno aterrizar (reutilizar la lista de `/tree`). **Ojo:** debe coordinar precedencia con el `Esc` único que cancela el run en vuelo (#68, ya hecho) — el double-Esc solo se interpreta en idle.

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
**Archivo:** `phoson_cli/fullscreen/app.py::_build_layout` · **Esfuerzo:** M · **Impacto:** 🔴 · **Estado:** en integración

**Problema doble:**
1. `TextArea(height=1, multiline=False)` — no hay forma de escribir prompts largos ni pegar bloques de código multi-línea. El TUI Textual descartado sí tenía Shift+Enter; la TUI actual perdió esa capacidad en la migración.
2. `InMemoryHistory()` — el historial de inputs muere al cerrar; el REPL clásico sí persiste en `~/.phoson/history.txt`. Inconsistencia injustificable entre frontends.

**Propuesta.**
- `TextArea(height=DYNAMIC)` con altura dinámica (crece hasta N líneas, luego scroll interno). prompt_toolkit soporta `height=DYNAMIC` con `char="..."`.
- Bindings: `Enter` envía (comportamiento actual intacto); `Ctrl+J` inserta newline. Se eligió `Ctrl+J` porque Shift/Ctrl/Alt+Enter no se mapean de forma portable en el parser VT100 de prompt_toolkit. El footer documenta el binding.
- Historial: reemplazar `InMemoryHistory` por `FileHistory("~/.phoson/history.txt")` (mismo archivo que el clásico → historial compartido). `↑/↓` navega entradas previas cuando el cursor está en la primera línea (prompt_toolkit ya hace esto si no compite con el scroll).
- Opcional (fase 2 del ítem): binding `Ctrl+E` para abrir el input actual en `$EDITOR` (patrón de Claude Code/Aider para prompts largos).

**Criterio de listo.**
- `Ctrl+J` inserta newline y Enter envía; test de bindings cubre ambos.
- Al reiniciar la TUI, `↑` recupera el último mensaje de la sesión anterior.
- El footer refleja el hint correcto según estado.

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
**Archivo:** `phoson_cli/fullscreen/app.py::submit` · **Esfuerzo:** S · **Impacto:** 🟡→🟠 (barato pero mejora la percepción) · **Estado:** en integración

**Problema.** `_is_run_in_flight()` descarta el envío en silencio: el usuario escribe, da Enter, no pasa nada, y no sabe si el programa colgó, ignoró el texto o hay que reintentar.

**Propuesta.** Cuando esté en vuelo:
- Notificar con `sink.notify("warn", "A turn is already running — Esc to cancel it first.")`, y
- Guardar el texto en el buffer (no limpiarlo), para que el usuario no pierda lo escrito.

Extra barato: mostrar en el header el estado ya existente (`status_text()` devuelve "Streaming"/"Running tool") con un spinner animado junto al status para que sea obvio sin leer.

**Criterio de listo.** Test: submit durante run → buffer conserva texto y sink recibió el warn.

---

# P1 — Máximo salto competitivo

Después de P0, estas son las mejoras que más acercan phoson-cli a la experiencia de las herramientas líderes.

---

### C1 — Panel de herramientas en vivo (PR-2 del TODO)
**Esfuerzo:** M · **Impacto:** 🔴

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

---

### C2 — Comandos P1 faltantes
**Esfuerzo:** M · **Impacto:** 🔴

Tres comandos que el propio TODO ya identifica como P1 y que completan el control del runtime:

1. **`/compact`** — disparador manual de la compactación. Hoy `SummarizationMiddleware` auto-corre a 80% de la ventana sin control del usuario. Implementación:
   - Con argumento opcional (`/compact keep last 10`) a futuro; v1: fuerza resumen ahora y muestra tokens antes→después.
   - Requiere exponer un método del summarizer (`force_compact(path) -> (before, after)`) y un evento de feedback en el sink.
2. **`/status`** — vista única que reemplace los cuatro atomizados (`/env`, `/cost`, `/tokens`, `/steps`): provider · model · cwd · sesión · costo acumulado · tokens usados/ventana · pasos · MCP servers activos · permisos activos. El estado de sandbox se añadirá solo después de implementar la fase 2 diferida de A1. Renderizable como tabla Rich compacta. Mantener los comandos viejos como aliases (no romper scripts).
3. **`/resume <id>`** — carga directa por id (hoy solo vía picker). Trivial sobre `load_session` existente; autocomplete con `SessionsArgCompleter` ya preparado para esto.

**Criterio de listo.** `/compact` reduce los tokens estimados y muestra el delta; `/status` imprime todas las dimensiones listadas; `/resume <id-parcial>` carga la sesión correcta (match por prefijo).

---

### C3 — Web tools (`web_search`, `web_fetch`)
**Esfuerzo:** M · **Impacto:** 🔴

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
**Esfuerzo:** S-M · **Impacto:** 🟠

Completar el PR-3 pendiente del TODO — el último tercio del plan look & feel:

1. **Status bar persistente** (footer enriquecido): `model · provider · $cost sesión · tokens used/window · cwd · MCP n · permisos activos`. El estado de sandbox se añadirá solo después de implementar la fase 2 diferida de A1. Ya casi todo existe disperso en header/prompt; consolidarlo en una línea inferior fija y aligerar el header (dejar marca + status).
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
**Esfuerzo:** M · **Impacto:** 🟡

Checklist concreto encontrado en la auditoría:

- [ ] **Eliminar `phoson_cli/textual/`** — directorio vacío con solo `__pycache__` (residuo de v0.7.0). Un `git rm -r`.
- [ ] **Unificar `_PROVIDER_LABELS`** — duplicado en `provider_picker.py` e `installer.py`. Extraer a `models.py` o un módulo compartido.
- [ ] **Unificar `_SPINNER_FRAMES`** — duplicado en `renderer.py` y `subagent_panel.py`. Extraer a un módulo `animations.py`.
- [ ] **Unificar `_token_indicator()`** — implementación idéntica en `repl.py` y `fullscreen/app.py`. Mover a `formatting.py` como función pura `format_token_indicator(used, window)`.
- [ ] **Unificar slash completers** — `_SlashCompleter` (repl.py) vs `SlashCompleter` (fullscreen/completer.py). El del fullscreen es más capaz; migrar y borrar el otro.
- [ ] **Eliminar `branch_session`** — deprecated no-op en DOS capas (repl + controller). Rompe API pública menor; hacerlo en la próxima minor con entrada en CHANGELOG.
- [ ] **Actualizar `TODO.md`** — fecha de cabecera (2026-08-20) desactualizada, LOC de repl.py incorrecto ("579", hoy 487). Este documento (`IMPROVEMENTS.md`) puede absorber/reemplazar parte del TODO.

**Criterio de listo.** `grep -rn "_PROVIDER_LABELS\|_SPINNER_FRAMES\|_token_indicator\|branch_session" phoson_cli/` devuelve una sola definición por símbolo (o cero para branch_session).

---

### D2 — Consolidar el REPL clásico o darle salida
**Esfuerzo:** S-M · **Impacto:** 🟠

**Problema.** `__main__.py` lanza siempre `PhosonApp`; `repl.py` (487 LOC) + `renderer.py` (635 LOC) + `ClassicSink` solo son alcanzables desde tests. Son ~1.100 LOC de frontend duplicado cuya lógica **ya diverge** (el clásico imprime `render_start_line`, el fullscreen no; spinners en threads vs ticker async).

**Dos opciones (elegir una):**

- **Opción A (recomendada): dar salida al clásico como modo degradado.** Flag `--classic` (o auto-detección: terminal sin capabilities full-screen, `TERM=dumb`, SSH legacy). Valor real: entornos donde la TUI full-screen no funciona bien, debugging, y mantiene vivos los tests de Renderer. Coste: ~1 línea en main + docs.
- **Opción B: congelarlo.** Marcarlo como test-only en docs, mover a `tests/helpers/` o documentar explícitamente "no user-facing". Ahorra mantenimiento mental pero desperdicia el trabajo hecho.

En ambos casos: extraer las diferencias de comportamiento (render_start_line) a decisiones conscientes y documentadas.

**Criterio de listo.** Opción A: `phoson-cli --classic` arranca el REPL clásico funcionando end-to-end. Opción B: docstring + docs indicando su estatus.

---

### D3 — Corregir Ctrl+V y soporte macOS clipboard
**Archivos:** `fullscreen/clipboard.py`, `keys.py` · **Esfuerzo:** S · **Impacto:** 🟠

**Dos problemas:**
1. **Sin macOS**: `read_clipboard_image()` solo prueba `wl-paste` (Wayland) y `xclip` (X11). En Mac, Ctrl+V de imagen falla silenciosamente ("No image on the clipboard"). Añadir `osascript`/`pngpaste` (con instrucción de instalar `brew install pngpaste`) o `pbpaste` para detección y mensaje específico por plataforma.
2. **Conflicto potencial con paste de texto**: Ctrl+V está rebind-eado globalmente a "pegar imagen", pero `TextArea` tiene paste de texto nativo con Ctrl+V. Comportamiento correcto: intentar imagen primero; si el clipboard no contiene imagen, delegar el paste de texto nativo (no tragar el evento). Verificar que el routing real funcione (los tests llaman a `app.paste_image()` directo — no cubren el conflicto).

**Criterio de listo.** En Linux: Ctrl+V con imagen en clipboard pega placeholder `[image #N]`; con texto en clipboard pega el texto. En macOS: mismo comportamiento o mensaje claro "install pngpaste". Tests del routing con eventos mock.

---

### D4 — Tests e2e/visuales de la TUI
**Esfuerzo:** M-L · **Impacto:** 🟠

**Qué falta.** La cobertura unitaria de la shell es buena (~62 tests), pero no existe ningún test que ejecute la `Application` real: el routing de teclas, el mouse handler (`_on_chat_mouse`), el ticker de subagentes, el double-KI del clásico (reconocido en TODO P2) y el render visual completo no están cubiertos. Los bugs de este tipo son precisamente los que aparecen en producción (ej.: el bug de Kitty/Alacritty con Shift+dígitos ya sufrido en v0.6.0).

**Propuesta escalonada:**
1. **Barato primero**: tests de routing real de bindings usando `Application` + `pipe_input` de prompt_toolkit (existe infraestructura de testing en prompt_toolkit: `create_pipe_input`). Cubrir: Enter→submit, Esc→cancel, Ctrl+C idle-vs-running, Shift+Enter→newline (tras A2).
2. **Golden ANSI snapshots**: renderizar `render_chat()` ante estados fijos del sink y comparar contra snapshots versionados (detecta regresiones visuales de tema/layout). Herramienta: simple fixture + archivos `.ansi` esperados.
3. **Smoke headless CI**: un test que arranque `PhosonApp` contra un chat mock y ejecute un turno completo programáticamente (ya existe el mock de `build_chat` en los tests actuales — extenderlo a ciclo completo con sink assertions).

**Criterio de listo.** Los 5 bindings críticos tienen test de routing real; 3+ golden snapshots (transcript vacío, con streaming, con error) en CI.

---

### D5 — Flags CLI faltantes
**Archivo:** `__main__.py` (212 LOC, parsing manual) · **Esfuerzo:** S · **Impacto:** 🟠

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

**Criterio de listo.** Cada flag con test unitario del parsing y efecto verificado; `--version` imprime y sale 0.

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
**Esfuerzo:** L · **Impacto:** 🔴 (para tareas largas)

El summarizer actual (auto a 80%) es funcional pero primitivo frente al SOTA:
- **Retained reasoning** (Codex): al compactar, conservar un resumen de la cadena de razonamiento, no solo del contenido — mejora mucho la continuidad en tareas largas (OpenAI reporta 13.3%→38.3% en ARC-AGI-3 con esta técnica sola).
- **Compaction estructurada**: en vez de resumen libre, generar artefactos tipo feature-list JSON + progress notes (patrón Anthropic long-running agents) que el siguiente segmento de contexto pueda consumir de forma fiable.
- **Offload de tool outputs grandes**: outputs >N KB van a disco con head/tail en contexto + path de referencia (patrón Claude Code).
- **Control fino**: `/compact aggressive|balanced|off`, umbrales configurables, preview del resumen antes de aplicar.

Depende de: C2 (`/compact`) como base de UI.

---

### E2 — Panel de subagentes con métricas en vivo
**Esfuerzo:** M · **Impacto:** 🟠

Hoy el panel en vivo muestra solo spinners ("waiting"); tiempo/tokens/costo aparecen recién en el summary final (el wire format `--- METRICS: ...` llega al terminar). Propuesta: streaming incremental del bloque METRICS (emitir línea partial cada N segundos desde el subagent) para alimentar las columnas Time/Tokens/Cost en vivo. Requiere tocar el protocolo producer-side en `tools/subagent.py` y el parser (que ya es tolerante a parciales — ventaja).

---

### E3 — Autocompletado de rutas y `@file` mentions
**Esfuerzo:** M · **Impacto:** 🟠

Patrón estándar (Cursor, Amp, Claude Code @-mentions): escribir `@src/` despliega archivos del repo filtrables fuzzy; el mention se expande a contenido adjunto (o referencia contextual). Implementable con un `PathCompleter` custom sobre el merge_completers existente + expansión en `controller._build_user_message`. Complementa A2 (multilinea) para flujos de "arregla el bug en @src/foo.py".

---

### E4 — Themes interactivos y auto-detección light/dark
**Esfuerzo:** S · **Impacto:** 🟢

- Auto-detección: consultar `COLORFGBG` / query OSC 11 al terminal para sugerir light vs dark la primera vez (confirmar y persistir). Muchos terminales modernos lo reportan.
- `/theme` picker con preview en vivo del banner y una muestra de cada token (aprovechar BasePicker).

---

### E5 — Check de updates al arrancar
**Esfuerzo:** S · **Impacto:** 🟢

La infraestructura existe (`updater.py`, check PyPI offline-safe). Añadir check asíncrono no-bloqueante al inicio (una vez cada 24h, cache en `~/.phoson/last_update_check`); si hay versión nueva, una línea discreta en el footer/header: "⬆ v0.8.1 available — /update". Nunca bloquear el paint.

---

### E6 — Keybindings personalizables
**Esfuerzo:** M · **Impacto:** 🟢

Sección `[keys]` en config.toml mapeando acciones → teclas (`toggle_reasoning = "c-t"`). Construir los bindings desde esa tabla en `keys.py`. Baja prioridad: el set actual es razonable y el costo de soportar remapeos (docs, conflictos, validación) supera el beneficio hasta tener base de usuarios pidiéndolo.

---

## Roadmap sugerido (secuencia de ataque)

```
Sprint 0–1 (P0, cerrado salvo integración)
├── B1/B2/B3 quick wins      ✅ v0.8.1
├── A1.fase1 permisos         ✅ PR #75
├── A3 AGENTS.md              ✅ PR #74
└── A2 multiline + history y A4 enter feedback  ← integración actual

Sprint 2 (competitividad, ~1-2 semanas)
├── C1 tool cards ricos
├── C2 /compact /status /resume
└── C4 status bar + help/tree

Sprint 3 (alcance, ~1-2 semanas)
├── C3 web tools
├── D5 flags CLI
└── D1 limpieza deuda

Continuo / paralelo
├── D2 destino del REPL clásico (decisión, luego 1 día)
├── D3 Ctrl+V cross-platform
├── D4 tests e2e (ir acumulando con cada sprint)
└── E1-E6 según demanda real de usuarios
```

## Principios para decidir durante la ejecución

1. **Todo formato nuevo va a `formatting.py` como renderable puro** — los dos frontends deben seguir compartiendo el 100%.
2. **Toda capability de seguridad es middleware/plugin**, no lógica en las tools — protege la filosofía framework-free y beneficia a Phoson-Core.
3. **Cada ítem termina con test + entrada en CHANGELOG** — la disciplina actual (616 tests) es la mayor fortaleza del repo; no erosionarla por velocidad.
4. **Medir antes de optimizar** — los números de perf existentes (~18× cache, 16fps throttle) vinieron de medir; los nuevos features con componente de render deben incluir su medición.
5. **Cuando dude entre añadir feature o pulir existente**: el comparativo de harnesses mostró que *menos tools pero mejores* gana (caso Vercel: −80% tools → 100% éxito). Preferir profundidad sobre amplitud.

---

*Documento generado a partir de la auditoría completa del repo (agosto 2026). Actualizar checkboxes al cerrar cada ítem.*
