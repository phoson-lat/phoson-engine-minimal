# ROADMAP — phoson-engine-minimal / phoson-cli

> **Actualizado:** 2026-09-04 · estado de referencia **v0.26.2** (v0.26.2: #182 `@import` confinado al repo + #183 `web_fetch` SSRF/limite de descarga + #184 system prompt del sub-agente + `agents` no ofrecido + #185 cinco fixes CLI + #200 plugin spec degradado a warning — PR #196, `7d5cf6c` + PR #199, `602217e` + PR #198, `5a99f91` + PR #197, `ced2d51` + PR #200, `25a2bb8`; v0.26.1: #178 `stop_reason` normalizado + truncación/enforcement + `result.truncated` y #177 `RetryingChat` en `build_chat` + `RetryMiddleware` deprecado — PR #195, `2507df0` + PR #194, `6080ba0`; v0.26.0: #179 `patch_file` unicidad + CRLF + #180 `cat -n`/caps/descripciones/system prompt ACI — PR #193, `67368b0`; v0.25.1: #175 `is_simple_shell_command` + `match_args` obligatorio — PR #192, `af502f0`) · 2164 tests pasados · ruff limpio (pyright: 1 error preexistente en `gemini.py`).
>
> **Fuentes:** los 33 issues abiertos en GitHub (17 previos + 16 abiertos el 2026-09-01 a partir de la revisión final; #138 cerrado el mismo día), `REVISION-FINAL-BY-FABLE.md` (hallazgos `F-nn`, verificados en código), `IMPROVEMENTS.md` (H-*/I-*), `IMPROVEMENTS-TUI.md` (T-*), `ISSUES-COMPLEXITY.md` (orden transversal previo).
>
> **Qué cambia respecto al orden anterior:** se inserta un **Sprint A de corrección y seguridad** antes de la infraestructura del harness (H-1/H-2). Razón: la revisión final encontró bugs con reproducción determinista (permisos no adjuntos en sub-agentes y one-shot, compactación que rompe pares tool_use/tool_result, retry que nunca se ejecuta, `patch_file` sin unicidad) que no son hipótesis de harness y no necesitan un gate de no-regresión para justificarse. La regla "medido contra H-1" sigue aplicando a doom loops, sandwich, budget en contexto y compact tool.
>
> El ROADMAP anterior (semana 10–16 de agosto, migración de Phoson-Core a este engine) quedó **completo** y vive en git history (`git log --follow ROADMAP.md`).

---

## 1. Estado actual

| Área | Estado |
|---|---|
| Look del TUI (T-1…T-13) | ✅ Todo shipped v0.20.0–v0.23.0. T-11 (ADR renderer) cerrado. |
| Perf del TUI | ✅ T-14 (#171) shipped v0.24.0 (PR #173, `84e44b7`, incl. fix F-40 spinner). ✅ Follow-up #186 (F-41/42/44) shipped v0.24.1 (PR #190, `df2fd70`). T-15 (#172) pendiente. |
| Harness infra (H-1/H-2) | Sin empezar (#139, #140). `bench/` tiene 4 tareas triviales, sin baseline, sin CI. |
| Seguridad | ✅ #183 (SSRF) + #182 (`@import` fuera del repo) shipped v0.26.2 (PR #199, `602217e` + PR #196, `7d5cf6c`). ✅ #174 + #141 shipped v0.24.2 (PR #191, `1f3f2d6`). ✅ #175 (allow-patterns `fnmatch` sobre comando completo) shipped v0.25.1 (PR #192, `af502f0`): `is_simple_shell_command` + `pattern_allows` (solo un simple command matchea un patrón bash; separadores/`$( `)/subshell ⇒ caen al nivel del tool) + `match_args` obligatorio sin fallback. 35 tests. |
| Loop | ✅ #178 (`stop_reason` ignorado, excepciones huérfanas) shipped v0.26.1 (PR #195, `2507df0`). ✅ #177 (retry no conectado) shipped v0.26.1 (PR #194, `6080ba0`). ✅ #176 (compactación rompe pares) shipped v0.25.0. |
| ACI (edit/search/prompt) | 🟠 #181 (grep/glob). ✅ #179 + #180 shipped v0.26.0 (PR #193, `67368b0`): unicidad de `patch_file` + CRLF + `cat -n` + caps + descripciones + secciones ACI del system prompt. |
| Notificación (#167) | ✅ Shipped v0.25.0. `notify_on_completion` (off/bell/desktop) + `/notify`; TTY-gated. |
| Deriva docs↔GitHub | #138 estaba resuelto en código y en docs pero abierto en GitHub; **cerrado 2026-09-01** tras verificar `f1b3d04` + 5 tests. Regla propuesta para #146. |

---

## 2. Tabla unificada de issues abiertos

Ordenada por sprint. `F-nn` remite a `REVISION-FINAL-BY-FABLE.md` §2; `H-n`/`T-n` a `IMPROVEMENTS.md` / `IMPROVEMENTS-TUI.md`. Severidad: 🔴 alta · 🟠 media · 🟡 baja/perf/deuda. Esfuerzo: S/M/L.

| Issue | Título corto | Origen | Sev. | Esf. | Sprint | Notas de cruce |
|---|---|---|---|---|---|---|
| [#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138) | Bench ignora `--model/--provider` | H-0 | — | — | ✅ **cerrado** | Fix en `f1b3d04` (main); verificado y cerrado el 2026-09-01. Deriva docs↔GitHub que #146 debería detectar. |
| [#171](https://github.com/phoson-lat/phoson-engine-minimal/issues/171) | T-14 windowing del chat pane | T-14 | 🟠 perf | M | ✅ **cerrado** | Merged en PR #173 (`84e44b7`); F-40 corregido en `40c8022`. Follow-up F-41/42/44 en #186. |
| [#186](https://github.com/phoson-lat/phoson-engine-minimal/issues/186) | TUI: ANSI/OSC crudo en bash, fingerprint sin generación, docstring O(visible) | F-41, F-42, F-44 | 🟠 | S | ✅ **cerrado** | Shipped v0.24.1 (PR #190, `df2fd70`, cerrado 2026-09-01): `Text.from_ansi` + strip OSC en `_bash_output_body`, `generation` en fingerprint de `_compute_chat_bounds`, docstring/CHANGELOG corregidos. 5 tests nuevos. |
| [#174](https://github.com/phoson-lat/phoson-engine-minimal/issues/174) | Sub-agentes y one-shot sin `PermissionMiddleware` ni `safe_mode` | F-01, F-02 | 🔴 | S-M | ✅ **cerrado** | Shipped v0.24.2 (PR #191, `1f3f2d6`): helper compartido `build_middlewares`/`build_summarizer`/`build_offload`; sub-agentes heredan la cadena + contexto fresco (safe_mode/bash_confirmation/plugin_ui); one-shot construye la cadena (fail-closed) e imprime `""` no `None`. 12 tests. Absorbía #141; prerrequisito de #129 slice 5. |
| [#141](https://github.com/phoson-lat/phoson-engine-minimal/issues/141) | Wall-clock en one-shot (`PHOSON_RUN_BUDGET_SECONDS`) | H-7 | 🟠 | S | ✅ **cerrado** | Shipped v0.24.2 (mismo PR #191): `run_budget_seconds` (600s, `0`=sin límite) + `PHOSON_RUN_BUDGET_SECONDS`; `asyncio.wait_for` sobre el run → exit 124 con mensaje limpio; interactivo no cambia. |
| [#175](https://github.com/phoson-lat/phoson-engine-minimal/issues/175) | Allow-patterns: `fnmatch` permite `git status; rm -rf /` | F-03, F-07 · Antigravity V-01 | 🔴 | S | ✅ **cerrado** | Shipped v0.25.1 (PR #192, `af502f0`): `pattern_allows`/`is_simple_shell_command` (solo un simple command matchea; `;`/`&`/`|`/`$( `/subshell ⇒ nivel del tool, quotes respetadas) + `match_args` obligatorio (sin fallback a "primer string"): `write_file` matchea `path`, no `content`. 35 tests. #169 ya no hereda el bypass. |
| [#167](https://github.com/phoson-lat/phoson-engine-minimal/issues/167) | Notificación al terminar (BEL / OSC 9/777) | externo | 🟡 | S | ✅ **cerrado** | Shipped v0.25.0: `notify_on_completion` (off/bell/desktop) + `PHOSON_NOTIFY_ON_COMPLETION` + `/notify`; TTY-gated, solo en run exitoso. Default `off`. |
| [#179](https://github.com/phoson-lat/phoson-engine-minimal/issues/179) | `patch_file` edita la primera de varias coincidencias | F-20 | 🔴 | S | ✅ **cerrado** | Shipped v0.26.0 (PR #193, `67368b0`): `count>1` sin `replace_all` ⇒ error con líneas (no escribe); hint difflib + CRLF/LF si `count==0`; lee bytes crudos (preserva CRLF) y normaliza EOL de la ancla. Primer candidato a medir con #139. |
| [#180](https://github.com/phoson-lat/phoson-engine-minimal/issues/180) | ACI: `read_file` con números de línea, descripciones, system prompt | F-21a, F-22, F-25 | 🟠 | M | ✅ **cerrado** | Shipped v0.26.0 (PR #193, `67368b0`): `read_file` `cat -n` + caps en rangos; `list_dir` 500; descripciones reescritas (8 tools); system prompt + Tool usage / Environment (git) / Safety (cache-friendly). Medición H-1 (#139) pendiente. |
| [#176](https://github.com/phoson-lat/phoson-engine-minimal/issues/176) | Compactación rompe pares tool_use/tool_result | F-10, F-11 | 🔴 | M | ✅ **cerrado** | Shipped v0.25.0: `safe_cut_index` en los 4 cortes (auto/emergency/manual + plan), resumen vacío ⇒ abortar, llamada de resumen tool-free (chat), 400 de pairing ⇒ error explícito. Desbloquea #147. |
| [#177](https://github.com/phoson-lat/phoson-engine-minimal/issues/177) | Retry inexistente (`RetryMiddleware` no reintenta; `RetryingChat` sin conectar) | F-12 | 🔴 | S | ✅ **cerrado** | Shipped v0.26.1 (PR #194, `6080ba0`): `build_chat` envuelve el adapter en `RetryingChat` (backoff 1s/×2/30s + jitter, solo pre-token ⇒ no duplica); `llm_max_attempts` (3, `1` desactiva) + `PHOSON_LLM_MAX_ATTEMPTS`; `RetryMiddleware` deprecado (warning). |
| [#178](https://github.com/phoson-lat/phoson-engine-minimal/issues/178) | `stop_reason` ignorado; excepciones dejan tool_use huérfano | F-13, F-14 | 🟠 | S-M | ✅ **cerrado** | Shipped v0.26.1 (PR #195, `2507df0`): `LLMDoneEvent.stop_reason` normalizado en los 19 adapters; el handler no se invoca con args `_truncated`/`_raw` (error accionable); `except Exception` en ToolRunner ⇒ `tool_result` emparejado con el tipo de la excepción; `result.truncated` + badge "⚠ truncated". 55 tests. |
| [#182](https://github.com/phoson-lat/phoson-engine-minimal/issues/182) | `@import` de AGENTS.md resuelve rutas fuera del repo | F-04 | 🟠 | S | ✅ **cerrado** | Shipped v0.26.2 (PR #196, `7d5cf6c`): root de confinamiento por archivo (repo root / `~/.phoson/`) aplicado **tras** `resolve()` ⇒ absolute/`..`/symlink fuera ⇒ marcador `[import refused: outside repo: …]`; `~` solo en el global. 8 tests. |
| [#183](https://github.com/phoson-lat/phoson-engine-minimal/issues/183) | `web_fetch` sin filtro SSRF ni límite de descarga | F-06 | 🟠 | S | ✅ **cerrado** | Shipped v0.26.2 (PR #199, `602217e`): `assert_public_url` pre-flight + hook por redirect (loopback/RFC1918/ULA/link-local + metadata IP/CGNAT/reservados); `stream()` + corte ~2 MB; prefijo "untrusted" en todo resultado. 25 tests. Relacionado con #144 (trifecta letal). |
| [#184](https://github.com/phoson-lat/phoson-engine-minimal/issues/184) | Sub-agentes sin system prompt; `agents` anunciado al hijo pero falla | F-23, F-24 | 🟠 | S | ✅ **cerrado** | Shipped v0.26.2 (PR #198, `5a99f91`): el hijo recibe el mismo `build_system_prompt` del padre (derivado de **su propio** subset de tools) + preamble; `ModelConfig.system` en todos los adapters; `_select_tools` quita `agent` **y** `agents` (recursión acotada por diseño, un nivel). 5 tests. |
| [#185](https://github.com/phoson-lat/phoson-engine-minimal/issues/185) | Varios pequeños CLI: `/resume` tokens, `/compact` sin persistir, `_resolve_bool`, `/mcp config`, updater | F-34…F-38 | 🟡 | S | ✅ **cerrado** | Shipped v0.26.2 (PR #197, `ced2d51`): split input/output persistido; `/compact` persiste + refresca header; bool/int estrictos + env-only keys no persistidas; `expanduser` + `0o600` en `mcps.json`; timeout 600 s en updater + estado real en `plugin list` + re-enable `path:`. 30 tests. Residuos fuera de alcance (respawn en `/mcp toggle` stdio; opt-out del check PyPI): deuda. |
| [#181](https://github.com/phoson-lat/phoson-engine-minimal/issues/181) | Tools nativas `grep` y `glob` | F-21b | 🟠 | M | **C** | Hipótesis de harness: shippear con descripción cuidada y medir contra #139. |
| [#140](https://github.com/phoson-lat/phoson-engine-minimal/issues/140) | `phoson_plugin_otel` (trazas) | H-2 | 🔴 | M | **C** | Slice 1 (trace-file JSON) antes de #139. |
| [#139](https://github.com/phoson-lat/phoson-engine-minimal/issues/139) | Eval set 15–25 tareas + gate nightly | H-1 | 🔴 | M | **C** | Incluir tareas de ancla ambigua, búsqueda en repo, compactación. Baseline = v0.24.0. |
| [#172](https://github.com/phoson-lat/phoson-engine-minimal/issues/172) | T-15 FormattedText desde el renderer | T-15 | 🟡 perf | S-M | **C** | Después de #173. Antes de #187 para no chocar. |
| [#134](https://github.com/phoson-lat/phoson-engine-minimal/issues/134) | Preserved thinking | I-134 | 🟠 | M | **D** | Confirmado en código. Prerrequisito de #145. |
| [#145](https://github.com/phoson-lat/phoson-engine-minimal/issues/145) | Reasoning sandwich | H-5 | 🟠 | M | **D** | Hipótesis; medir contra #139. |
| [#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142) | Doom loop detection | H-3 | 🟠 | S-M | **D** | Hipótesis; medir contra #139. |
| [#143](https://github.com/phoson-lat/phoson-engine-minimal/issues/143) | Contexto ambiental (step N/M, tiempo, % ventana) | H-4 | 🟠 | S | **D** | Se alimenta del budget de #141. |
| [#144](https://github.com/phoson-lat/phoson-engine-minimal/issues/144) | Permisos por intención + MCP hints + audit log | H-6 | 🔴 | M-L | **D** (Fase 2) · tras #139 (Fases 1+3) | #174/#175 son bugs del mecanismo actual y van antes. |
| [#146](https://github.com/phoson-lat/phoson-engine-minimal/issues/146) | Paridad docs↔código↔GitHub en CI | H-8 | 🟡 | S-M | **E** | Añadir regla: "✅ resuelto" en docs ⇒ issue cerrado (caso #138). |
| [#147](https://github.com/phoson-lat/phoson-engine-minimal/issues/147) | `compact_context()` controlada por el agente | H-9 | 🟡 | M | ✅ **resuelto** | Shipped v0.25.0: tool sin args en el engine principal (no en el registro de sub-agents), mismo handoff estructurado que `/compact`/auto (`safe_cut_index` + resumen estructurado + abort si vacío), splice in-flight in-place + evento de rebase al fin del run, anunciada en system prompt (solo cuando existe) + `docs/cli/compaction.md`. Default `allow`. 11 tests. **Pendiente:** medición contra H-1 (#139, Sprint 4). |
| [#148](https://github.com/phoson-lat/phoson-engine-minimal/issues/148) | Tool budget / carga diferida | H-11 | 🟡 | análisis | **E** | Con datos de #140. |
| [#129](https://github.com/phoson-lat/phoson-engine-minimal/issues/129) | Background agents (6 slices) | I-129 | 🟡 | L | **E** (slices 1+2) | Requiere #141 y #174 resueltos (unattended sin controles). |
| [#187](https://github.com/phoson-lat/phoson-engine-minimal/issues/187) | Refactor: extraer `ChatPane`/`HeaderModel`/floats/`RewindController` de `app.py` | F-45 | 🟡 | M | **E** | Después de #172. |
| [#188](https://github.com/phoson-lat/phoson-engine-minimal/issues/188) | Adaptadores incompletos (Bedrock, Mistral, Gemini, Ollama) | F-50 · Antigravity §2.4 | 🟡 | M | **E** | Ollama `<think>` se relaciona con #134. |
| [#189](https://github.com/phoson-lat/phoson-engine-minimal/issues/189) | Offload sin retención; Postgres O(N) por guardado | F-51, F-52 · Antigravity V-02/V-05 | 🟡 | S | **E** | |
| [#149](https://github.com/phoson-lat/phoson-engine-minimal/issues/149) | Handoff multi-sesión | H-10 | 🟡 | L | Diferido | Cuando #129 slice 4 lo pida. |
| [#169](https://github.com/phoson-lat/phoson-engine-minimal/issues/169) | Plugin SSH | externo | 🟡 | M | Diferido / externo | Requiere #175 antes para no heredar el bypass. |

**Hallazgos de la revisión sin issue propio** (se resuelven dentro de los anteriores o son notas): F-05 (confinamiento de rutas; decisión de producto, ver #180), F-08 (skills de checkout no confiable; nota en docs), F-15/F-16/F-17/F-19 (loop, menores; anotar en #178 si se atacan), F-18 (tools en paralelo; deuda, Sprint E), F-26 (encoding en `read_file`/`patch_file`; incluir en #179), F-30…F-33 (REPL clásico y rebuild; anotar en #185 si se atacan), F-53 (token drift; opinión, no medida).

---

## 3. Plan por sprints

Cada sprint es una release. Un PR por fila, con test de regresión. No mezclar PRs de seguridad con PRs de look o perf.

### Sprint A — corrección y seguridad (v0.24.0 + v0.24.1 + v0.24.2)

```
1. Mergear PR #173 (T-14 + fix F-40).                                        ✅ merged 84e44b7 (v0.24.0)
2. Cerrar #138 en GitHub.                                                    ✅ hecho
3. #186   TUI follow-up: strip de control codes en bash, generación en el
          fingerprint, docstring/CHANGELOG.                                    ✅ shipped v0.24.1 (PR #190, df2fd70)
4. #174 + #141   Fronteras: helper compartido de middlewares; sub-agentes
          heredan Permission + safe_mode + confirmation (fail-closed sin
          callback); one-shot construye Offload → Summarizer → Permission;
          PHOSON_RUN_BUDGET_SECONDS. (#184 puede ir aquí: misma construcción.) S-M
          ✅ shipped v0.24.2 (PR #191, 1f3f2d6): helper `session_utils.build_middlewares`/
          `build_summarizer`/`build_offload`; `subagent.py` hereda cadena + contexto
          fresco (safe_mode/bash_confirmation/plugin_ui) en `agent` y `agents`;
          `_run_oneshot` construye la cadena (fail-closed, imprime `""` no `None`) y
          aplica el budget vía `asyncio.wait_for` → exit 124. 12 tests nuevos.
5. #175   Allow-patterns: solo un *simple command* matchea un patrón bash
          (`;`, `&`/`&&`, `|`/`||`, newline, subshell, `` ` ``/`$( `) ⇒ no
          match ⇒ cae al nivel del tool (ask/deny); quotes respetadas
          (`git commit -m 'a; b'` es un comando). `match_args` obligatorio:
          sin entrada explícita no hay match text (ni fallback a "primer
          string"), y la tabla CLI declara bash→command, file tools→path,
          web_search→query, web_fetch→url. Grants "[a] always" sujetos a la
          misma regla. Doc en `docs/cli/permissions.md`.                     S
          ✅ shipped v0.25.1 (PR #192, `af502f0`): `permissions.py`
          `is_simple_shell_command` + `pattern_allows` en `policy.check` y
          en los session grants (también cubre la línea `!` del TUI, que
          llama a `policy.check` directo); `permissions_store.MATCH_ARGS`
          con 7 tools. 35 tests (matriz compound/simple, session grant
          compound, `write_file` path vs content). Suite 2012 passed.
6. #167   BEL + OSC 9/777; config notify_on_completion.                         S
          ✅ shipped v0.25.0: `notify.py` + `notify_on_completion`
          (off/bell/desktop) + env + `/notify`; TTY-gated; solo en run exitoso.
          29 tests.
```

**Criterio de listo del sprint:** un tool en `deny` es rechazado desde un sub-agente y desde `-p` (test); `git *` no aprueba `git status; rm -rf /` (test); un run `-p` con tool colgado termina en el presupuesto con exit ≠ 0 (test).

### Sprint B — v0.25.0–v0.26.2 · ACI y robustez del loop (COMPLETO)

```
1. #179 + #180   Edit tool seguro + read_file cat -n + caps + descripciones +
          system prompt con guía de tools, git status y aviso de no confiable. M
          ✅ shipped v0.26.0 (PR #193, `67368b0`): unicidad de
          `patch_file` (count>1 ⇒ error con líneas, no escribe; hint difflib +
          CRLF/LF si count==0), lee bytes crudos (preserva CRLF, normaliza EOL
          de la ancla), no-UTF-8 ⇒ error accionable; `read_file` `cat -n` +
          cap en rangos + hint del siguiente rango; `list_dir` 500;
          descripciones reescritas (read/write/patch/list/bash/agent/agents/
          web_fetch); system prompt + Tool usage (gated) / Environment
          (git branch + status capped, solo en repo) / Safety. 19 tests.
          Medición H-1 (#139) pendiente (Sprint C).
2. #176   Corte de compactación en fronteras seguras; resumen vacío aborta;
          tools=[] en el call_next del resumen.                                 M
          ✅ shipped v0.25.0: `safe_cut_index` en los 4 cortes (auto/
          emergency/build_compaction + /compact manual y su plan); resumen
          vacío ⇒ abortar (no perder historial); llamada de resumen tool-free
          vía `chat`; 400 de pairing ⇒ error explícito (no se traga). 17 tests.
          Desbloquea #147.
3. #177   RetryingChat en build_chat; deprecar RetryMiddleware.                  S
          ✅ shipped v0.26.1 (PR #194, `6080ba0`): `build_chat`
          envuelve el adapter en `RetryingChat` (backoff 1s/×2/30s + jitter,
          solo pre-token ⇒ no duplica); `llm_max_attempts` default 3
          (`1` desactiva) + `PHOSON_LLM_MAX_ATTEMPTS` (+ config.toml);
          retry logueado (`phoson_cli.retry`); `RetryMiddleware` deprecado
          (warning). 8 tests nuevos.
4. #178   stop_reason normalizado; except Exception en ToolRunner con backfill. S-M
          ✅ shipped v0.26.1 (PR #195, `2507df0`):
          `LLMDoneEvent.stop_reason` normalizado (end_turn/max_tokens/
          tool_use/refusal/pause_turn/other; ausente→None, unknown→other) en
          los 19 adapters (loop OpenAI-compartido = 15; + anthropic/ollama/
          bedrock/gemini/mistral). `max_tokens` mid tool-call ⇒ el adapter
          marca `_truncated` y el ToolRunner NO invoca el handler: responde el
          tool_use con error accionable (retry smaller / divide) y empareja el
          historial; también cubre el fallback `_raw` (el viejo `fn(_raw=...)`
          → TypeError opaco). `except Exception` en la invocación del handler +
          `on_after_tool` ⇒ `tool_result` con el tipo de la excepción
          (`{name}: {exc_type}: {detail}`), el run sigue y el historial queda
          válido para el proveedor. `result.truncated` + badge "⚠ truncated" en
          `render_done_line` (REPL clásico y TUI). 55 tests nuevos.
5. #182, #183, #184, #185   Pequeños independientes (uno por PR).               S c/u
          ✅ shipped v0.26.2:
          - #182 (PR #196, `7d5cf6c`): `@import` confinado al árbol del repo
            (root por archivo, tras `resolve()`, marker de rechazo); 8 tests.
          - #183 (PR #199, `602217e`): `web_fetch` SSRF (pre-flight + por
            redirect, metadata/loopback/RFC1918/CGNAT) + corte ~2 MB + prefijo
            "untrusted"; 25 tests.
          - #184 (PR #198, `5a99f91`): system prompt del hijo (mismo builder,
            subset propio) + `agent`/`agents` nunca ofrecidos; 5 tests.
          - #185 (PR #197, `ced2d51`): los 5 fixes CLI, un test por fila;
            30 tests.
          - Bonus fuera de fila: #200 (PR #200, `25a2bb8`): una spec de plugin
            inválida degrada a warning y el CLI arranca (no brickeaba todo el
            startup); 3 tests.
```

**Criterio de listo:** compactación con `min_keep` cayendo en un `tool_result` produce historial válido (test); 429 antes del primer token se reintenta (test); `patch_file` con dos coincidencias falla sin escribir (test).

### Sprint C — v0.26.3 · función de fitness

```
1. #181   grep + glob nativos.                                                  M
2. #140   slice 1: trace-file JSON por run (run → step → tool_call).            M
3. #139   bench/ a 15–25 tareas; baseline ≥3 corridas con modelo local fijo
          sobre v0.24.0 ("sin ACI"); nightly que falla si tasa < baseline −
          varianza. Primeros deltas a medir: #179/#180 y #181.                  M
4. #172   T-15 FormattedText.                                                   S-M
```

### Sprint D — v0.27.0 · razonamiento y middleware (todo medido contra #139)

```
1. #134   Preserved thinking.                                                   M
2. #145   Reasoning sandwich como hipótesis falsable.                           M
3. #142   Doom loop detection.                                                  S-M
4. #143   Contexto ambiental, alimentado por #141.                              S
5. #144   Fase 2: readOnlyHint/destructiveHint de MCP como señal.               S-M
```

### Sprint E — v0.28.0+ · autonomía, análisis y deuda

```
1. #146   Gate docs↔código↔GitHub (incluye "✅ en docs ⇒ issue cerrado").       S-M
2. #147   compact_context() — ✅ adelantado a v0.25.0 (se desbloqueó con #176,
          que también subió a v0.25.0). Medición contra H-1 (#139) pendiente.    M
3. #148   Análisis de tokens de definiciones con datos de #140.                 análisis
4. #129   Background agents slices 1+2.                                         M
5. #187   Split de app.py (después de #172).                                    M
6. #188   Adaptadores. #189 Offload/Postgres. F-18 gather de tools read-only.   M / S / S
```

### Diferido / externo

- #149 handoff multi-sesión — cuando #129 slice 4 lo pida.
- #144 Fases 1+3 — tras #139 con suite adversarial.
- #169 plugin SSH — contribución externa bienvenida; después de #175.

---

## 4. Reglas del orden

1. **Bugs no esperan al gate.** Sprints A y B se justifican con tests unitarios, no con el bench. Sprint D sí espera a #139.
2. **Seguridad en PRs separados** (#174/#141, #175): tocan `permissions.py`, `subagent.py`, `__main__.py`. No mezclar con perf ni look.
3. **#141 se resuelve dentro de #174.** Un solo PR construye la cadena de middlewares en one-shot y añade el budget.
4. **#140 slice 1 antes que #139**, como ya decía `ISSUES-COMPLEXITY.md`.
5. **#147 después de #176.** No exponer compactación al agente mientras el corte pueda producir un 400.
6. **Cada pieza nueva de harness (Sprint D) declara la asunción sobre el modelo que la justifica**, para retirarla cuando expire (regla heredada de `reporte-harness.md`).
7. **Al cerrar un issue, marcar ✅ aquí**, en `REVISION-FINAL-BY-FABLE.md` §2 si es F-*, y en `IMPROVEMENTS.md` si es H-*.

---

## 5. Ver también

- `REVISION-FINAL-BY-FABLE.md` — hallazgos `F-nn` con archivo:línea, verificación y número de issue; cruce con los reportes previos.
- `REVISION-BY-ANTIGRAVITY.md`, `reporte-harness.md` — revisiones anteriores (la primera sobre código, la segunda sobre docs).
- `IMPROVEMENTS.md` — board de H-*/I-* con análisis por ítem.
- `IMPROVEMENTS-TUI.md` — look del TUI (todo shipped) y ADR T-11.
- `ISSUES-COMPLEXITY.md` — orden transversal previo; este ROADMAP lo reemplaza como cola de ataque.
- `TODO.md` — índice histórico de decisiones anteriores a v0.10.
- [`Phoson-Core/ROADMAP.md`](../Phoson-Core/ROADMAP.md) — lado consumidor de la migración (etapa cerrada).
