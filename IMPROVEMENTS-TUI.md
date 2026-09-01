# IMPROVEMENTS-TUI — look del frontend de `phoson-cli`

> **Origen:** revisión crítica del look (no del harness) contra el SOTA de agent TUIs 2025–2026 (Claude Code, OpenCode, Codex CLI), más un ADR de renderer: alternativas **Python** y **ligeras** a `prompt_toolkit`.
>
> **Cómo usar este documento:** la sección A es trabajo de dirección visual **sobre el stack actual** (Rich + prompt_toolkit). La sección B es la decisión de toolkit — no mezclarlas. Un P0 de look no justifica un rewrite del TUI.
>
> **Estado de referencia:** v0.20.0 · frontend default = `phoson_cli/fullscreen` (`prompt_toolkit.Application` + Rich→ANSI) · clásico retenido (`--classic` / `TERM=dumb`) · one-shot = stdout Rich.
>
> **Criterio de toda la revisión:** modelo congelado, ¿el primer frame convencería a alguien que ya usa Claude Code o OpenCode? Hoy no. El engine y el glue de UI están por encima del look.

---

## Tesis

El frontend es un **REPL con alt-screen bien ingenierizado**, no un **agent harness de 2026**. Por dentro hay craft real (tokens de tema, cache ANSI I-84, tool cards, composing I-128, OSC 8, rewind, costos live). Por fuera se ve como un producto que todavía se presenta a sí mismo: ASCII, plugins oficiales, “Online”, frases tipo “Chewing on that…”, un `Panel` de reasoning y un composer que es un `TextArea` con `❯`.

El SOTA de este género **no es “chat bonito en la terminal”**. Es una superficie de trabajo donde:

| Principio | Claude Code / OpenCode / Codex | Phoson hoy |
|-----------|--------------------------------|------------|
| Transcript-first | Chrome de 1 línea; el trabajo es el producto | Header ticker + footer mural + banner |
| Composer como objeto | Caja, placeholder, chip de modelo/modo, `@` `/` `!` | `❯` + `TextArea` |
| Tools como cards | Familia visual, collapse, diff con fondo | Un `⚙` para todo; diff solo recolorea `+/-` |
| Permiso = modo | `Shift+Tab` ask / auto / plan | Modal `Run bash command?` |
| Thinking secundario | `Thought for 12s`, colapsado | `Panel` ROUNDED del tamaño de la respuesta |
| Tema = el terminal | `system` + Catppuccin/Tokyo/Nord/JSON | Purple `#120d1d` que pisa el fondo del usuario |
| Color semántico | Accent en 1–2 sitios | Accent en arte, header, spinner, tools, label, frames |
| Empty state | Recientes, `/init`, 2 prompts | ASCII + “Official plugins” |
| Espera | `Thinking 8s` | Copy rotativo de chatbot |

Ingeniería de UI: ~8/10. Look de harness: ~5/10. La brecha es **dirección**, no capacidad. El P0 se cierra **sin cambiar de librería**.

---

## Tabla resumen

### A. Look (mismo stack)

| ID | GitHub | Título | Prioridad | Esfuerzo | Impacto | Decisión |
|----|--------|--------|-----------|----------|---------|----------|
| **T-1** | [#151](https://github.com/phoson-lat/phoson-engine-minimal/issues/151) | Sacar ASCII y publicidad de plugins del transcript | **P0** | S | 🔴 El primer frame deja de ser un README | Sprint look 0 |
| **T-2** | [#152](https://github.com/phoson-lat/phoson-engine-minimal/issues/152) | Matar “Online”, label “Phoson” por turno, badges con background | **P0** | S | 🔴 El transcript se lee como trabajo, no como chat | Sprint look 0 |
| **T-3** | [#153](https://github.com/phoson-lat/phoson-engine-minimal/issues/153) | Reasoning default colapsado a una línea mute; adiós `Panel` | **P0** | S | 🔴 El scratchpad deja de competir con la respuesta | Sprint look 0 |
| **T-4** | [#154](https://github.com/phoson-lat/phoson-engine-minimal/issues/154) | Composer: caja 1-char, placeholder `@` `/` `!`, una sola regla | **P0** | S-M | 🔴 El 40% del tiempo de pantalla deja de ser un prompt de shell | Sprint look 0 |
| **T-5** | [#155](https://github.com/phoson-lat/phoson-engine-minimal/issues/155) | Activity line = `Thinking 8s`; borrar frases rotativas | **P0** | S | 🟠 Ansiedad de espera; olor a chatbot | ✅ Hecho (v0.20.0): `CurrentTurn.thinking_since` (monotónico) → `Thinking {n}s` por reloj de pared; re-arma a 0 por episodio (tool start / freeze); glifo braille intacto; frases y ticks de rotación eliminados |
| **T-6** | [#156](https://github.com/phoson-lat/phoson-engine-minimal/issues/156) | Chip de modo `ask`/`auto` + confirmación como card (Yes / Always / No) | **P1** | M | 🔴 Sin esto no se lee como harness | ✅ Hecho (Sprint look 1): chip en header (1 s cache), `Shift+Tab` cicla y persiste; card `run_float_bash_card` con comando mono + `[y]/[a]/[n]`; *Always* persiste patrón glob-quoted |
| **T-7** | [#157](https://github.com/phoson-lat/phoson-engine-minimal/issues/157) | Tool cards: glifo por familia, collapse, diff con fondo, `created` vs `updated` | **P1** | M | 🟠 Las tools *son* el producto | ✅ Hecho (Sprint look 1): glifos 📖📂🖼/✍🪄/⌘/🔎🔗/📜 (cards + spinner clásico); diff `+`/`−` con fondo (tokens `diff_add_bg`/`diff_del_bg`); `write_file` dice Created/Updated; paths como OSC 8 `file://`; `/details` toggla el cuerpo colapsado en place |
| **T-8** | [#158](https://github.com/phoson-lat/phoson-engine-minimal/issues/158) | Tema `system` (hereda fg/bg del terminal) + JSON en `~/.phoson/themes/` | **P1** | M | 🟠 Deja de pelearse con Gruvbox/Catppuccin | ✅ Hecho (Sprint look 1): tier `system` **default** sin `on #rrggbb` (accent = spinner/focus cyan); JSON drop-in en `~/.phoson/themes/*.json` (base + tokens, aparece en `/theme`); la pregunta light/dark de E4 queda obsoleta (el terminal ya resuelve) |
| **T-9** | [#159](https://github.com/phoson-lat/phoson-engine-minimal/issues/159) | Footer contextual (3 hints) + scrollbar sin flechas | **P1** | S | 🟡 Discoverability real, no cheatsheet cortada | ✅ Hecho (Sprint look 1): footer 3-hints por estado (idle/running/picker); Shift+Drag → `/keys` + docs; scrollbar sin flechas |
| **T-10** | [#160](https://github.com/phoson-lat/phoson-engine-minimal/issues/160) | Hero tape con un **diff**, no un research dump + Ctrl+T | **P2** | S | 🟡 El README vende el producto correcto | ✅ Hecho (post-v0.23.0): hero regenerado con VHS 0.11 — tape `app.conf` (read → `patch_file` con diff `+/−` visible → respuesta → composer idle); sin ASCII, sin "Online", sin research dump; `tui.gif` 7.8 MB → 250 KB |
| **T-12** | [#162](https://github.com/phoson-lat/phoson-engine-minimal/issues/162) | Command palette + `!` bash (gestos SOTA, no look puro) | **P2** | M | 🟡 Paridad de interacción con OpenCode/Claude | ✅ Hecho (v0.23.0): `Ctrl+P` abre picker fuzzy (native + plugin slash commands) reusando `model_picker._fuzzy_score` y el scaffolding de Float; `!cmd` corre en el shell gateado por la misma bash permission policy (ask → card T-6, `load_policy` re-leído en run-time) y el output entra como bash tool card normal (`add_bash_card`) |
| **T-13** | *(nuevo, 2026-08-31)* | Chip de reasoning effort en header + `Ctrl+E` cicla `off→…→max` (patrón T-6) | **P1** | S | 🟡 La perilla existe (`/reasoning-effort`) pero no se ve ni se toca sin slash | ✅ Hecho (v0.20.0): chip `effort: high` / `· effort off` en header; `Ctrl+E` cicla y persiste (`save_config`), aplica al siguiente run; remapeable en `[keys]`, aparece en `/keys` |

### B. Toolkit (no mezclar con A)

| ID | GitHub | Título | Prioridad | Esfuerzo | Impacto | Decisión |
|----|--------|--------|-----------|----------|---------|----------|
| **T-11** | [#161](https://github.com/phoson-lat/phoson-engine-minimal/issues/161) | ADR de renderer: qué hacer con `prompt_toolkit` | **P2** | S (el ADR) / L (si se cambia) | 🟡 El techo estético a 12 meses | **Diferido.** Spike opcional *después* de T-1…T-9. Veredicto abajo: **quedarse**. |

---

## A. Detalle — look sobre el stack actual

### T-1 — Sacar ASCII y plugins del transcript

* **Área:** `phoson_cli/_views.py::render_banner`, `phoson_cli/fullscreen/app.py` (`self.sink.blocks.append(self._banner_block)`), plugins que publican un bloque de “official plugins” al arrancar.
* **Problema:** el empty state del código (`Type a message and press Enter.` en `fullscreen/render.py`) **nunca existe** porque el banner de 17 líneas (`phos-ascii.txt` + “terminal agent”) se inyecta como bloque del chat. En el hero (`assets/tui.png`) encima va `phoson_plugin_* Official plugins: checkpoint, MCP, memory`. Claude Code no te recuerda que tiene MCP en cada sesión.
* **Solución:**
  - No appendar `_banner_block` al sink. `/about` (o el primer run de una instalación, una vez) muestra el arte.
  - Los plugins oficiales no se anuncian en el transcript; `/plugins` y `/help` ya existen.
  - Empty state real: 2–3 sesiones recientes **o** un hint de una línea (`@ files  ·  / commands  ·  ! bash`). Nada de mascota.
* **Criterio de listo:** al abrir `phoson-cli` en un cwd sin historial, el pane no contiene el ASCII ni la palabra “Official”. Test de sink: `blocks` vacío o solo empty-state. Regenerar `assets/tui.gif` *después* de T-10.

### T-2 — Chrome y transcript secos

* **Área:** `fullscreen/app.py::_get_header_text` (`Online` vía `sink.status_text`), `formatting.py::render_assistant_label` (`"Phoson"`), `render_user_turn` / `render_start_line` / `render_history` (badges `on #23192f` / `on #3a255e`).
* **Problema:** cuatro lenguajes de transcript (badge user, palabra Phoson, badge assistant en history, rail `│ ⚙`). Header al mismo peso visual (`header_dim`) para marca, modelo, provider, cwd, tokens, USD, attachments, agents.md, monitors, update, status. “Online” es vocabulario de IM.
* **Solución:**
  - Header: `model  cwd  29k/262k` (coste solo si `> 0`). Status idle = nada, o el **modo** (T-6), nunca “Online”.
  - User: gutter `›` + texto. Sin chip con background.
  - Assistant: markdown, sin firmar cada turno con el nombre del producto.
  - History replay usa las mismas primitivas que el turno vivo (hoy `render_history` es otro dialecto: badge + `Rule`).
* **Criterio de listo:** captura del pane idle sin la palabra “Online” ni “Phoson” como label de turno. `render_user_turn` no usa `badge_user` con `on #`. Tests de formatting actualizados.

### T-3 — Reasoning como metadato

* **Área:** `formatting.py::render_reasoning_panel` (`box.ROUNDED`), `fullscreen/sink.py` (`show_reasoning_default=True`), `render_streaming_panel` (reasoning inline en grey42).
* **Problema:** el panel del screenshot oficial es un anti-patrón — caja grande, prosa de scratchpad, compite en área con la respuesta. En 2026 el thinking no es contenido.
* **Solución:**
  - Default **colapsado**: una línea mute `thought 8s` (elapsed; el sink ya tiene timestamps de turno).
  - Ctrl+T expande in-place, **sin** `Panel`.
  - Al terminar el turno, auto-collapse.
  - `render_reasoning_panel` se queda para el clásico si hace falta, no es el path fullscreen.
* **Criterio de listo:** con `show_reasoning` default, un turno con reasoning no abre caja. Test: el bloque finalizado es 1 línea hasta toggle.

### T-4 — Composer como objeto

* **Área:** `fullscreen/app.py::_build_layout` (`TextArea(prompt="❯ ")`, `_INPUT_MAX_LINES = 5`, dos `Window(char="─"|"—")`).
* **Problema:** es un prompt de shell. SOTA (Claude/OpenCode): caja, placeholder, chip de modelo/modo. Hay dos reglas horizontales distintas (`─` y `—`).
* **Solución (dentro de prompt_toolkit):**
  - Una sola regla, o un `Frame` de 1 char alrededor del input (`frame.border` ya existe en `_apply_style`).
  - Placeholder: `Ask anything  ·  @ files  ·  / commands` (`TextArea` soporta `placeholder=`).
  - Quitar `❯` o dejarlo *dentro* de la caja, no como prompt de shell.
  - Documentar por qué newline es Ctrl+J (ya está en código); no es P0 inventar Shift+Enter portable.
* **Criterio de listo:** screenshot del composer idle muestra placeholder y un solo separador. Tests de layout no asumen `❯ ` como único prompt.

### T-5 — Espera con elapsed, no personalidad ✅ *released (v0.20.0)*

* **Área:** `fullscreen/sink.py::_THINKING_PHRASES`, `activity_text()`, `tick_activity_frame()`.
* **Problema:** “Pondering the problem… / Chewing on that… / Almost there…” es stall de 2023. El tiempo es el único copy que reduce ansiedad.
* **Solución:** `Thinking 8s` (monotonic desde `begin_activity`). El glifo braille se queda (estándar; la decisión de congelarlo en streaming es correcta, I-84). Borrar `_THINKING_PHRASES`.
* **Criterio de listo:** ningún test ni snapshot contiene “Pondering” / “Chewing”. La línea cambia cada segundo (`8s` → `9s`), no cada 2.5 s a otra frase.
* **Hecho:** `CurrentTurn.thinking_since` (monotónico) se arma al primer render de la fase thinking y se re-arma a 0 en cada episodio (tool start / freeze de streaming) — el contador mide *la* espera actual, no el run. `activity_text()` rinde `Thinking {int(elapsed)}s` (truncado a segundos); el número piggybacks en los repaints del tick de glifo (cero costo extra). `_THINKING_PHRASES`, `_THINKING_PHRASE_TICKS` y `thinking_phrase_index` eliminados.

### T-6 — Modo visible + confirmación como card

* **Área:** `permissions_store.py`, `fullscreen/confirmation.py` (`Run bash command? {command!r}`), header/composer.
* **Problema:** el `permissions_store` puede ser sólido; si no se ve, no existe. SOTA de Claude: card con el comando/diff, Yes / Don’t ask again / No, y `Shift+Tab` cicla Normal → Auto-Accept → Plan. El modo vive en el chrome.
* **Solución (look, no rehacer permisos):**
  - Chip `ask` | `auto` en composer o header, leído del store. `Shift+Tab` cicla los dos. Plan puede ser v2.
  - `confirm_bash` pinta una card (comando en mono, tres acciones), no un string de modal genérico. Reusar `run_float_confirm` con un renderer propio, no un yes/no ciego.
* **Criterio de listo:** idle muestra el chip. Un bash en `ask` muestra el comando y Always. Test de confirmation no busca el string `Run bash command?`.

### T-7 — Tool cards al lenguaje del género

* **Área:** `formatting.py` (`_TOOL_VERBS`, `tool_icon` default `"⚙"`, `_diff_body`, `_write_summary_body` que **siempre** dice `created`, `_BASH_PREVIEW_LINES = 6`, `_DIFF_MAX_LINES = 20`).
* **Problema:** un glifo para read/write/bash/web; no hay collapse (OpenCode tiene `/details`); el diff recolorea el prefijo, sin `diffAddedBg`; `write_file` miente en overwrite; los paths no son OSC 8.
* **Solución:**
  - Glifos por familia (`read_file`/`list_dir`/`view_image` · `write_file`/`patch_file` · `bash` · `web_*` · `skill` · plugin specs ya existen).
  - Card colapsada por default (header + ✓/✗ · duration); expandir con tecla o `/details`.
  - Diff: token `ok`/`err` **y** un fondo sutil (`on #1a2` / `on #2a1` en dark; en `system`, ANSI reverse). Truncate igual.
  - `write_file`: `created` si el archivo no existía, `updated` si sí (el tool ya tiene el path).
  - Path como OSC 8 `file://` (el passthrough de `hyperlinks.py` ya existe).
* **Criterio de listo:** snapshot de `patch_file` tiene líneas `+`/`-` con fondo. Test de `write_file` distingue created/updated. `tool_icon("read_file") != tool_icon("bash")`.

### T-8 — Tema `system` + JSON drop-in

* **Área:** `phoson_cli/theme.py` (4 tiers, `panel_bg="on #120d1d"`, accent everywhere), `terminal_theme.py` (OSC 11 ya detecta light/dark y **solo sugiere**).
* **Contraste SOTA:** OpenCode tiene `system` (`text`/`background` = `none`), 10+ temas de editor, JSON en `~/.config/opencode/themes/`. El `tui.tape` oficial usa `Set Theme "GruvboxDark"` — el purple de marca **ya se ve mal en el hero**.
* **Solución:**
  - Nuevo tier `system`: `text=""`, `panel_bg=""`, badges sin `on #`, accent reducido a spinner + focus. El terminal pinta el resto.
  - Default nuevo: `system` si no hay `PHOSON_THEME` / `config.toml`. OSC 11 deja de *preguntar* light vs dark para el default (el terminal ya lo resolvió).
  - JSON en `~/.phoson/themes/*.json` (tokens del dataclass `Theme`). Los plugins Python (`ThemeExtension`) se quedan para casos raros.
  - Markdown: no pelear con `code_theme="monokai"` sobre un terminal Catppuccin — `system` usa `ansi_dark` / `ansi_light` o el default de Rich.
* **Criterio de listo:** `PHOSON_THEME=system phoson-cli` no emite `on #rrggbb` en el header/composer. Un JSON mínimo en `~/.phoson/themes/nord.json` aparece en `/theme`. Tests de `load_theme` cubren `system`.

### T-9 — Footer contextual

* **Área:** `_FOOTER_HINT` en `fullscreen/app.py` (8 atajos en una línea que se corta en 80 cols, incluido `[Shift+Drag] Select text`).
* **Solución:** 3 hints según estado. Idle: `enter send  ·  ctrl+j newline  ·  / commands`. Running: `esc cancel`. Picker: `enter  ·  esc`. Shift+Drag se queda en `docs/cli/mouse-and-links.md` y `/keys`. ScrollbarMargin sin flechas, o nada (el wheel ya funciona).
* **Criterio de listo:** a 80 columnas el footer no se trunca. Test del hint idle vs running.

### T-10 — Hero que vende un harness ✅ *done (post-v0.23.0)*

* **Área:** `assets/tui.tape` (prompt de research GitHub + Ctrl+T).
* **Solución:** tape de un `patch_file` (diff visible) + composer idle. Sin ASCII, sin reasoning panel, sin “Official plugins”. Regenerar `tui.gif` / `tui.png` **después** de T-1…T-5.
* **Criterio de listo:** el PNG del README muestra una tool card de edición, no un dump de markdown de GitHub.
* **Hecho:** el hero viejo (era I-115, pre-sprint) mostraba un research dump de GitHub contra el chrome viejo ("Online", badges). Nuevo tape: `app.conf` de 4 líneas → prompt "Update app.conf to use port 9090 and enable debug mode" → `reading file` + `✎ editing file` con el diff `+/−` coloreado (T-7) → respuesta con el before/after → `▸ thought Ns` (T-3) → composer idle (T-4). El `Ctrl+T` de expandir reasoning quedó fuera: el diff ya es el foco y expandir el scratchpad en el hero añade ruido. Regenerado contra vLLM local con VHS 0.11 + ttyd 1.7.7 (wrapper `env -i` del `assets/README.md`); `tui.png` = último frame. Bonus: `demo.gif` (one-shot) también era pre-sprint y se refrescó con el mismo pipeline; `tui.gif` bajó de 7.8 MB a 250 KB al reducir el run de 30 s a ~10 s.

### T-12 — Gestos SOTA (después del look) ✅ *released (v0.23.0)*

Command palette (`Ctrl+P` unifica `/model` `/theme` `/sessions` / slash) y `!` bash (Claude/OpenCode: el output entra al transcript). No son look; son el siguiente escalón de interacción. No abrirlos hasta que T-4 y T-6 existan — si no, se construyen sobre un composer/permiso que vamos a tirar.

* **Área:** `palette_picker.py` (nuevo: `PaletteEntry`/`PalettePickerResult`/`build_command_palette` + fuzzy + paginado), `fullscreen/app.py` (dispatch de `!` en `submit`, `open_command_palette`/`_run_command_palette*`/`_run_bash_line`), `fullscreen/keys.py` + `config.py` (action `command_palette`, default `c-p`), `fullscreen/sink.py` (`add_bash_card`).
* **Hecho:** la fuente del palette es `catalog.specs` (una sola lista cubre `CommandSpec` native y `CliCommandSpec` de plugins, que comparten `names`/`primary`/`help`). `↑/↓` + `PageUp/Down` navegan, `enter` ejecuta vía el path normal `/command` (args vacíos), `esc` cierra; `/exit` desde el palette sale de la app, consistente con tipearlo a mano. La carrera de doble-`Ctrl+P` se cierra con `self._palette_open` síncrono (el flag se arma en `open_command_palette` y se libera en el `finally`, porque `_active_float` solo se settea dentro de la task de fondo). `!` relee la policy con `load_policy()` en run-time (el ciclo `ask`/`auto` de `Shift+Tab` se respeta sin cache stale); el output se renderiza con `render_tool_done_line` — una bash tool card idéntica a la del agente — para que `/details` la pueda re-plegar. La detección de fallo de infra usa `re.fullmatch` de las dos formas exactas que devuelve `_run_bash` (timeout / spawn), no `startswith`, para no clasificar como ✗ un output legítimo que solo *empiece* por esas frases.

### T-13 — Reasoning effort visible + ciclable ✅ *released (v0.20.0)*

* **Área:** `fullscreen/app.py` (chip del header + `cycle_reasoning_effort`), `fullscreen/keys.py` (action `cycle_reasoning_effort`), `config.py` (`KNOWN_KEY_ACTIONS`).
* **Problema:** la perilla de reasoning effort existía (`/reasoning-effort <low|…|max|off>`, persistida en `config.toml`) pero era invisible: el header no la mostraba y cambiarla requería un slash command — la misma brecha que T-6 resuelve para los permisos.
* **Solución (patrón T-6):** chip en el header (`effort: high` en accent / `· effort off` en dim) + `Ctrl+E` (mnemónico *E* = effort; `Ctrl+T` ya es el toggle de visibilidad del reasoning) que cicla `off → low → medium → high → xhigh → max → off`, muta `config.reasoning_effort`, persiste con `save_config(..., only_fields={"reasoning_effort"})` e invalida la cache del header. El run lo lee en `run_turn` (`ModelConfig`), así que aplica al **siguiente** turno — un run en vuelo no se ve afectado. Table-driven → remapeable en `[keys]` y visible en `/keys` sin código extra.
* **Criterio de listo:** el header refleja el effort al instante; el ciclo cubre los 5 niveles + off y persiste; no toca el toggle de visibilidad (Ctrl+T); el keymap sigue validándose contra `KNOWN_KEY_ACTIONS`.

---

## B. T-11 — ADR: ¿qué hacemos con `prompt_toolkit`?

### Contexto (verificado en código)

El controller **ya está desacoplado** (`phoson_cli/ui_protocols.py::AgentEventSink`). Los formatters son Rich puro (`formatting.py`, “no Console, no Live, no threads”). El acoplamiento a prompt_toolkit es el **shell**:

- Fullscreen: `Application` + `HSplit` + `TextArea` + floats (`pickers/_base.py`, confirmation, model/theme/session).
- Clásico: `PromptSession` (mismo history file, mismos completers).
- Puente sucio: Rich `Console` → ANSI → `prompt_toolkit.formatted_text.ANSI`, con hack OSC 8 (`hyperlinks.py`, SOH/STX).
- Deps actuales: `prompt-toolkit` (~3.4 MB, solo extra `wcwidth`) + `rich` (~2.6 MB, `markdown-it-py` + `pygments`).
- Binarios: `release-binaries.yml` empaqueta wheels **pure-Python**. Una lib nativa (Zig/C) cambia ese pipeline.

El techo estético de este puente está documentado en el propio código (mouse tracking binario, `ANSI()` no entiende OSC 8, `stream_plain` → Markdown hace layout shift, `ScrollbarMargin` con flechas). Se puede llegar a un **8/10 de look** aquí (T-1…T-9). No a un 10.

“Ligero” en *este* repo significa, en orden: (1) pure Python, (2) sin wheels nativos extra (rompe el release actual), (3) cabe en “engine minimal”, (4) async-nativo (el loop del agente es asyncio), (5) composer + markdown + tool cards, no un widget kit de formularios.

### Alternativas Python, de más ligera a más pesada

| Toolkit | Peso real | Async | Composer / markdown / cards | Estado 2026 | ¿Sirve a un harness? | Veredicto |
|---------|-----------|-------|-----------------------------|-------------|----------------------|-----------|
| **stdlib `curses` / `blessed`** | Mínimo (`blessed` es termcap; `curses` va en stdlib) | Lo escribes tú | Lo escribes tú | Vivos | Solo si reescribimos el TUI entero | **No.** Coste L×3 para recuperar lo que ya tenemos (keys, history, floats, completers). |
| **picotui** | 16 KB, 0 deps | No | Widgets de diálogo; sin markdown, sin geometry, sin double-buffer | Último PyPI **2021**; “experimental WIP” | No | **Descartado.** Ligero de verdad e inútil para streaming + asyncio. |
| **pytermgui** | Puro Python, ~widgets+TIM | Loop propio | Widgets / ventanas, no transcript | **Archivado**, “final release” | No | **Descartado.** Muerto. |
| **urwid** | ~200 KB, pocas deps | Sí (con adapter) | Canvas 80×24; markdown/cards DIY | Vivo, look 2004; **LGPL** | No (licencia + estética) | **Descartado.** LGPL choca con binarios MIT; no hay techo de look. |
| **npyscreen / asciimatics** | curses / pesado de animación | No / a medias | Formularios, no chat | Estancados | No | **Descartado.** |
| **Rich `Live` + `PromptSession` (el clásico)** | **0 deps nuevas** | Sí | Transcript nativo (scrollback del terminal), composer de PT, cards Rich | Ya está en el árbol (`--classic`) | Sí, es el modelo **xli / Codex-mínimo** | **Opción ligera real.** No es un reemplazo: es *dejar de pelear el alt-screen*. |
| **xli (`python-xli`)** | 2 deps: **Rich + prompt_toolkit** (el mismo stack) | Sí | Hecho para agentes: cards mutables, `@`, `/`, approve Yes/Always/No, native scrollback, temas `codex`/`minimal` | Pre-1.0, ~28★ (2026) | Sí, **es exactamente este producto** | **No es alternativa a PT.** Es un glue *encima*. Evaluar como “¿borramos nuestro puente?” no como “¿cambiamos de renderer?”. |
| **prompt_toolkit + Rich (hoy)** | Ya pagado | Sí | Lo que hay; techo ~8/10 con T-1…T-9 | Vivo, IPython-scale | Sí | **Default. Quedarse.** |
| **Textual** | Pesado (CSS, widget tree, Rich debajo) | Sí | Widgets; el transcript se *construye*; markdown nativo (mismo autor que Rich) | Muy vivo | A medias: forma de **dashboard/IDE**, no de harness | **No.** Más peso, peor shape, choca con “minimal”. |
| **OpenTUI Python** (`opentui` / `pyopentui`) | **No ligero:** core Zig + nanobind + Yoga, wheels por plataforma | Sí | Diff, Markdown, Textarea nativos — el motor de **OpenCode** | Community, no oficial; `opentui-python` ~6★, `pyopentui` 0★ / 3 commits | Sí, es el techo 10/10 | **Diferido duro.** Única vía a look OpenCode *en Python*. Incompatible hoy con “minimal” + release pure-Python. Spike solo si el look es estrategia a 12 meses. |

Ink / Bubble Tea / Ratatui quedan fuera de alcance: el runtime es Python.

### Lo que “ligero” *no* es

- Textual no es ligero. Es el framework más pesado del cuadro y está diseñado para otra forma de app.
- OpenTUI no es ligero. Es nativo a propósito (por eso OpenCode se ve caro). Meter Zig en `phoson-engine-minimal` es una decisión de producto, no de dependencias.
- xli no quita peso: **añade** una capa pre-1.0 sobre las mismas dos libs, y **cambia la forma** (inline scrollback, no alt-screen). Eso es un cambio de UX, no un downsize.
- picotui / pytermgui / urwid *sí* son ligeros y *no* cubren composer multiline + streaming markdown + tool cards + mouse/OSC 8 + asyncio. Adoptarlos es reescribir el TUI a cambio de un look peor.

### Tres formas de producto (elegir una; no mezclar)

El SOTA no es un solo renderer. Son **dos shapes**:

1. **Alt-screen (OpenCode).** App a pantalla completa, chrome propio, mouse, panes. Es lo que `fullscreen/` ya es. Techo = el puente Rich→ANSI, o un salto a OpenTUI.
2. **Inline transcript (xli, Codex mínimo, el clásico).** El terminal conserva scrollback / select / find nativos. Solo vive la región inferior (composer, card in-flight, picker). **Ya existe** como `--classic`. El look SOTA de “no pelees el terminal” está más cerca de esto que de un `Application` a pantalla completa.
3. **Híbrido actual.** Alt-screen *y* Rich off-screen *y* OSC 8 hack. Es el peor de los dos para look (pierdes select nativo, ganas poco de app). T-1…T-9 lo dejan presentable; no lo convierten en OpenCode.

Recomendación de producto: **no cambiar de shape en el sprint de look.** El default sigue siendo fullscreen. El clásico se mantiene. Si después de T-1…T-9 el techo sigue doliendo, el spike no es picotui: es (a) **promover el modelo inline** (clásico o xli) como default, o (b) **OpenTUI** como apuesta de look.

### Decisión

1. **Ahora: quedarse en prompt_toolkit + Rich.** Ejecutar A (T-1…T-9). El 80% del gap vs Claude Code es dirección, no el toolkit.
2. **No adoptar** picotui, pytermgui, urwid, blessed-como-TUI, Textual, npyscreen.
3. **No adoptar xli en este ciclo.** Misma stack, API pre-1.0, shape distinta (pierde el alt-screen que *es* el default). Si el clásico se volviera el default, reevaluar xli como reemplazo del *glue*, no del renderer.
4. **OpenTUI Python: spike explícito, no dependencia.** Criterio para abrirlo: T-1…T-9 merged, el look sigue siendo queja #1 de usuarios, y aceptamos wheels nativos en `release-binaries.yml`. Hasta entonces es ruido.
5. **El ADR se reabre** si se decide que el default deja de ser alt-screen (entonces el clásico / xli ganan) o que el look es estrategia (entonces OpenTUI).

### Criterio de listo del ADR

Este archivo *es* el ADR. Listo cuando el equipo acuerda las 5 líneas de arriba (comentario en PR o en `IMPROVEMENTS.md` enlazando aquí). Un spike de OpenTUI, si ocurre, es un `docs/plans/T-11.md` aparte con un proto de composer+markdown+diff, **no** un rewrite del CLI.

---

## Orden de ataque

```
Sprint look 0 (P0, S–M, mismo stack)
  T-1  banner / plugins fuera del chat
  T-2  chrome + transcript secos
  T-3  reasoning colapsado
  T-5  Thinking Ns          ✅ (rama feat/tui-sprint-look-1)
  T-4  composer caja+placeholder

Sprint look 1 (P1) — HECHO (2026-08-31, 4 commits)
  T-9  footer contextual    ✅
  T-6  chip de modo + card de confirmación  ✅ (commit aparte, toca permisos)
  T-7  tool cards (glifo / collapse / diff bg / created|updated)  ✅
  T-8  tema system + JSON    ✅
  T-13 chip de reasoning effort + Ctrl+E  ✅ (patrón T-6)

Sprint look 2
  T-10 hero tape               ✅ (post-v0.23.0: `patch_file` + diff, VHS 0.11)
  T-12 palette + ! bash      ✅ (v0.23.0)
  T-11 se queda cerrado salvo que el look siga siendo el gap
```

T-1…T-5 caben en un PR denso o en 2 (chrome/transcript vs composer/activity). T-6 toca permisos: no mezclarlo con el PR de “secar el look”.

---

## Relación con `IMPROVEMENTS.md`

Esto **no** duplica issues de harness (H-*) ni I-129/I-134. Es look. Varios ítems de UI ya cerrados (I-83 errores 1-línea, I-84 CPU, I-128 composing, I-108 Alt+Backspace, E4 temas) son *craft*; este archivo es *dirección*. Si un T-* se abre en GitHub, el ID de issue se anota en la tabla A igual que H-* en `IMPROVEMENTS.md`.

---

## Nota final

El código de UI es de un equipo que ya piensa en SOTA de harness engineering. El look no recibió el mismo rigor. Claude Code se ve caro porque **quita**. OpenCode se ve caro porque **se pinta con el tema del usuario**. Phoson se ve junior porque **añade**: cara, purple, plugins, “Pondering”, “Online”, caja de reasoning.

Cambiar de toolkit no quita eso. T-1…T-5 sí.
