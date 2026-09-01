# ISSUES-COMPLEXITY — ranking de issues abiertos por complejidad de resolución

> **Origen:** revisión de los 17 issues abiertos de `phoson-lat/phoson-engine-minimal` (129, 134, 138–150, 151–162) leídos uno por uno, no solo por la etiqueta de esfuerzo.
>
> **Estado de referencia:** 2026-08-31 · v0.20.0 (T-5, T-6, T-7, T-8, T-9, T-13 released).
>
> **Criterio de la orden:** superficie de código tocada + dependencias + riesgo de regresión + dificultad del criterio de listo. "Complejo" aquí no es lo mismo que "P0" — #138 es P0 del plan de harness y es de los fixes más baratos del repo.
>
> **Cómo usar:** si un issue cambia de scope o se desbloquea una dependencia, el ranking se desactualiza. El "orden práctico sugerido" al final es el que importa para atacar, no los niveles.

---

## Resumen en una tabla (menor → mayor complejidad)

*Estado (2026-08-31):* ✅ = released en v0.20.0 (commits `39ceac6` T-9/T-7, `8667562` T-8, `3c6325c` T-6, `1ae8627` T-5, `09696d8` T-13) · 🔶 = parcial · 🔴 = abierto.

| Orden | Issue | Título (corto) | Nivel | Estado |
|-------|-------|----------------|-------|--------|
| 1 | [#155](https://github.com/phoson-lat/phoson-engine-minimal/issues/155) | T-5 — `Thinking 8s`, borrar frases rotativas | 1 | ✅ v0.20.0 |
| 2 | [#159](https://github.com/phoson-lat/phoson-engine-minimal/issues/159) | T-9 — Footer contextual (3 hints) | 1 | ✅ `39ceac6` |
| 3 | [#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138) | Bug bench: `--model`/`--provider` ignorados | 1 | ✅ |
| 4 | [#150](https://github.com/phoson-lat/phoson-engine-minimal/issues/150) | Model picker: duplicados + lista incompleta | 1 | 🔴 |
| 5 | [#151](https://github.com/phoson-lat/phoson-engine-minimal/issues/151) | T-1 — Banner/plugins fuera del transcript | 2 | 🔴 |
| 6 | [#152](https://github.com/phoson-lat/phoson-engine-minimal/issues/152) | T-2 — Chrome seco (matar "Online") | 2 | 🔴 |
| 7 | [#153](https://github.com/phoson-lat/phoson-engine-minimal/issues/153) | T-3 — Reasoning colapsado a 1 línea | 2 | 🔴 |
| 8 | [#154](https://github.com/phoson-lat/phoson-engine-minimal/issues/154) | T-4 — Composer como objeto | 2 | 🔴 |
| 9 | [#141](https://github.com/phoson-lat/phoson-engine-minimal/issues/141) | Run budget one-shot (`PHOSON_RUN_BUDGET_SECONDS`) | 3 | 🔴 |
| 10 | [#162](https://github.com/phoson-lat/phoson-engine-minimal/issues/162) | T-12 — Command palette + `!` bash (bloqueado por T-4/T-6) | 3 | 🔴 (T-6 ya resuelto; queda T-4) |
| 11 | [#156](https://github.com/phoson-lat/phoson-engine-minimal/issues/156) | T-6 — Chip de modo + card de confirmación | 3 | ✅ `3c6325c` |
| 12 | [#157](https://github.com/phoson-lat/phoson-engine-minimal/issues/157) | T-7 — Tool cards (glifos/collapse/diff bg) | 3 | ✅ `39ceac6` |
| 13 | [#158](https://github.com/phoson-lat/phoson-engine-minimal/issues/158) | T-8 — Tema `system` + JSON themes | 3 | ✅ `8667562` |
| 14 | [#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142) | Doom-loop middleware | 3 | 🔴 |
| 15 | [#143](https://github.com/phoson-lat/phoson-engine-minimal/issues/143) | Inyección de presupuesto al contexto | 3 | 🔴 |
| 16 | [#146](https://github.com/phoson-lat/phoson-engine-minimal/issues/146) | Docs parity gate + fix de derivas | 3 | 🔴 |
| 17 | [#140](https://github.com/phoson-lat/phoson-engine-minimal/issues/140) | Plugin OTel (slice 1: trace-file JSON) | 3 | 🔴 |
| 18 | [#147](https://github.com/phoson-lat/phoson-engine-minimal/issues/147) | `compact_context` tool | 3 | 🔴 |
| 19 | [#134](https://github.com/phoson-lat/phoson-engine-minimal/issues/134) | Preserved thinking | 4 | 🔴 |
| 20 | [#139](https://github.com/phoson-lat/phoson-engine-minimal/issues/139) | Set de evaluación + gate nightly (H-1) | 4 | 🔶 (substrato `bench/` commitado: runner + 4 tasks; faltan el gate y el resto del set) |
| 21 | [#144](https://github.com/phoson-lat/phoson-engine-minimal/issues/144) | Permisos por intención (3 fases) | 4 | 🔴 (T-6 resuelve el UI de permiso, no la taxonomía) |
| 22 | [#145](https://github.com/phoson-lat/phoson-engine-minimal/issues/145) | Reasoning sandwich (bloqueado por #134 + #139) | 4 | 🔴 |
| 23 | [#149](https://github.com/phoson-lat/phoson-engine-minimal/issues/149) | Handoff multi-sesión (diferido hasta #129) | 4 | 🔴 |
| 24 | [#129](https://github.com/phoson-lat/phoson-engine-minimal/issues/129) | Background agents (6 slices) | 4 | 🔴 |
| — | [#161](https://github.com/phoson-lat/phoson-engine-minimal/issues/161) | T-11 — ADR renderer (decisión, no código) | — | 🔶 (ADR escrito en `IMPROVEMENTS-TUI.md` §B; falta el acuerdo) |
| — | [#160](https://github.com/phoson-lat/phoson-engine-minimal/issues/160) | T-10 — Hero tape (secuencial: tras look 0+1) | 1 | 🔴 (los assets actuales son de I-115, pre-sprint) |
| — | [#148](https://github.com/phoson-lat/phoson-engine-minimal/issues/148) | Tool budget / lazy loading (hoy: "medir" vía #140) | — | 🔴 |

---

## Nivel 1 — Fixes chicos y aislados (cambios localizados, 1 PR)

### 1. [#155](https://github.com/phoson-lat/phoson-engine-minimal/issues/155) — T-5: `Thinking 8s` ✅ *released (v0.20.0)*
- Borrar `_THINKING_PHRASES` + timer monotónico en `fullscreen/sink.py`.
- Un solo archivo, sin API nueva. El glifo braille se queda.
- Criterio simple: snapshots sin "Pondering"/"Chewing"; la línea cambia cada segundo.
- **Hecho:** `CurrentTurn.thinking_since` (monotónico) arma el episodio al entrar en thinking; `activity_text()` rinde `Thinking {int(elapsed)}s` — el número corre por reloj de pared, no por ticks (el glifo sigue animado vía `tick_activity_frame`). Cada tool start / freeze de streaming reinicia el episodio a 0 (el contador mide *la* espera actual, no el run). Frases y `_THINKING_PHRASE_TICKS` eliminados.

### 2. [#159](https://github.com/phoson-lat/phoson-engine-minimal/issues/159) — T-9: Footer contextual ✅ *released v0.20.0 (`39ceac6`)*
- 3 strings de hint por estado (idle/running/picker) en `_FOOTER_HINT` de `app.py`.
- No cambia el layout. El trabajo real es quitar 5 atajos de la línea y moverlos a docs/`/keys`.
- **Hecho:** `_FOOTER_HINT_IDLE`/`_RUNNING`/`_PICKER` en `fullscreen/app.py`, seleccionados por estado del app.

### 3. [#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138) — Bug del bench ✅
- El fix recomendado (config efímero vía env en el workspace del runner) **no toca el CLI**: solo `bench/run_bench.py` + README + modelo registrado en el JSON de resultados.
- Es P0 del plan de harness por *impacto*, no por esfuerzo.
- **Hecho:** `_build_env()` inyecta las env reales `PHOSON_MODEL`/`PHOSON_PROVIDER` (que `config.py` resuelve env → config.toml → default) y **pop**ea las heredadas del shell del dev (una `PHOSON_MODEL` de la shell no puede re-apuntar una baseline); los `*_OVERRIDE` fantasma desaparecen. El JSON de resultados (`_results_payload`) ahora registra `model` + `provider` + `commit`. Smoke end-to-end: `--model openai/gpt-4o-mini --provider openrouter` corrió un LLM real y el JSON quedó con esos valores. 5 tests de regresión en `tests/test_bench_runner.py`.

### 4. [#150](https://github.com/phoson-lat/phoson-engine-minimal/issues/150) — Model picker
- Bug de dedup/key: modelos con el mismo nombre colapsan y faltan entries.
- Dos pickers (principal + sub-agent) comparten la lógica; fix localizado + test unitario.

## Nivel 2 — Look P0 (cambios visuales sin nueva arquitectura)

### 5. [#151](https://github.com/phoson-lat/phoson-engine-minimal/issues/151) — T-1: Banner/plugins fuera del chat
- Dejar de appender `_banner_block` + empty state real + quitar prints de plugins.
- El único riesgo: no romper el test de sink y el hero (que se regenera después en T-10).

### 6. [#152](https://github.com/phoson-lat/phoson-engine-minimal/issues/152) — T-2: Chrome seco
- Mucho detalle visual (header, badges, labels, dialecto de history), poca lógica nueva.
- Riesgo menor: es una regresión de *look*, se ve en una screenshot.

### 7. [#153](https://github.com/phoson-lat/phoson-engine-minimal/issues/153) — T-3: Reasoning colapsado
- Default + toggle Ctrl+T + auto-collapse: toca sink y formatter, reutiliza timestamps existentes.
- Cuidado con el path clásico que sigue usando el panel.

### 8. [#154](https://github.com/phoson-lat/phoson-engine-minimal/issues/154) — T-4: Composer como objeto
- Frame + placeholder en prompt_toolkit; el más "S-M" del sprint look 0.
- Tests de layout asumen `"❯ "`; hay que actualizarlos sin romper completers/`@file`.
- Es **base de T-12**, así que que quede bien diseñado importa.

## Nivel 3 — Features medianas

### 9. [#141](https://github.com/phoson-lat/phoson-engine-minimal/issues/141) — Run budget one-shot
- 1 env var + watchdog a nivel de run + teardown limpio con exit code.
- **Testable sin LLM** y no toca el modo interactivo (Esc sigue siendo el escape). El issue lo marca como "adelantable" precisamente por esto.

### 10. [#162](https://github.com/phoson-lat/phoson-engine-minimal/issues/162) — T-12: Palette + `!` bash
- Dos piezas (fuzzy picker unificado + pipe a bash) sobre infra existente.
- Complejidad *real* = complejidad propia + esperar: **bloqueado por T-4 y T-6** (si no, se construye sobre composer/permisos que van a cambiar).

### 11. [#156](https://github.com/phoson-lat/phoson-engine-minimal/issues/156) — T-6: Chip de modo + card de confirmación ✅ *released v0.20.0 (`3c6325c`)*
- UI sobre `permissions_store` (ya sólido) + keybinding `Shift+Tab` + renderer de confirmación.
- Toca permisos → debe ser PR propio, no mezclado con "secar el look".
- **Hecho:** chip de modo siempre visible en header (re-lee la política ≤1/s), `cycle_permission_mode` en Shift+Tab, y card de confirmación de bash con 3 acciones (Yes / Always / No) vía `FullScreenConfirmationService`.

### 12. [#157](https://github.com/phoson-lat/phoson-engine-minimal/issues/157) — T-7: Tool cards ✅ *released v0.20.0 (`39ceac6`)*
- 5 sub-features (glifos, collapse, diff bg, created/updated, OSC 8) todas en `formatting.py` + snapshots.
- El más granular del nivel: conviene partirlo en 2 PRs (glifos+created/updated · collapse+diff bg).
- **Hecho:** glifos por familia, collapse con `/details` + toggle, diff bg y marcado created/updated en `formatting.py`; los paths llevan OSC 8 real.

### 13. [#158](https://github.com/phoson-lat/phoson-engine-minimal/issues/158) — T-8: Tema `system` + JSON ✅ *released v0.20.0 (`8667562`)*
- Nuevo tier + loader JSON + **cambio de default**.
- El riesgo es el cambio de default: afecta a toda la superficie visual de los 4 tiers existentes; hay que verificar `no-color`, `ansi` y el auto-detect de OSC 11.
- **Hecho:** tier `system` como default (hereda fg/bg del terminal) + drop-in themes JSON en `~/.phoson/themes/` (`theme.py`).

### 14. [#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142) — Doom-loop middleware
- El middleware es fácil (los hooks `on_before_tool`/`on_after_tool` ya existen).
- Lo que sube el costo: normalización de args (whitespace, orden de keys) y el criterio "medido contra H-1" (no puede bajar la tasa de éxito — depende de #139).

### 15. [#143](https://github.com/phoson-lat/phoson-engine-minimal/issues/143) — Inyección de presupuesto al contexto
- Middleware `on_before_llm` chico, reutiliza el cálculo del gate I-91.
- Lo delicado: **no romper el prompt cache** (el bloque va al final; test de regresión de fracción cacheada) + medición contra H-1.

### 16. [#146](https://github.com/phoson-lat/phoson-engine-minimal/issues/146) — Docs parity gate
- El PR de docs (2 derivas de `TODO.md`) es trivial; el gate es el trabajo:
  `check_docs_parity.py` (comandos/flags/envs/plugins) + job de CI + tope de `AGENTS.md`.
- Costo sostenido: el script hay que mantenerlo cada vez que se agregue un comando o env var.

### 17. [#140](https://github.com/phoson-lat/phoson-engine-minimal/issues/140) — Plugin OTel
- "La parte difícil ya está hecha" (el stream de eventos es rico), pero es un plugin nuevo completo:
  mapeo de ~15 tipos de evento → spans jerárquicos, correlación por IDs, packaging, test de overhead.
- El **slice 1** (trace-file JSON local) es S y ya alcanza para el gate nightly de #139 — atacar por ahí.

### 18. [#147](https://github.com/phoson-lat/phoson-engine-minimal/issues/147) — `compact_context` tool
- La tool en sí envuelve `/compact` existente (M chico).
- El costo está en el diseño (documentar qué sobrevive, nunca reescritura libre) y en que su criterio de medición **requiere H-1 (#139)** → sprint 4.

## Nivel 4 — Grandes / multi-slice / dependientes

### 19. [#134](https://github.com/phoson-lat/phoson-engine-minimal/issues/134) — Preserved thinking
- M-L cross-cutting: schema (`Message` + reasoning), agent loop, **3 familias de adaptadores**
  (OpenAI-compat con `reasoning_content`/`preserve_thinking`, Anthropic con thinking blocks + signature,
  Ollama/vLLM), persistencia JSONL, cap policy, `TokenUsage.reasoning_tokens`.
- Cada adaptador tiene sus propias reglas de qué se devuelve; el criterio de listo toca todos.

### 20. [#139](https://github.com/phoson-lat/phoson-engine-minimal/issues/139) — Set de evaluación + gate nightly
- 15–25 tasks con checkers deterministas, baseline con ≥3 corridas (varianza medida),
  workflow nightly, split held-out documentado.
- **Es el P0 del que dependen los criterios de #142, #143, #145, #147** — su complejidad es la de su propia infra + ser el cuello de botella del plan.
- Bloqueante #138 (modelo fijo verificable) **resuelto** ✅ — queda armar el set + baseline + gate nightly.

### 21. [#144](https://github.com/phoson-lat/phoson-engine-minimal/issues/144) — Permisos por intención (3 fases)
- M-L: reescribe la taxonomía de riesgo (Fase 1), consume anotaciones MCP (Fase 2),
  audit log + anti-fatiga (Fase 3), migración de `permissions.json` y suite adversarial.
- Solo la **Fase 2 es S-M sin migración** — se puede atacar hoy como slice; 1 y 3 requieren H-1.

### 22. [#145](https://github.com/phoson-lat/phoson-engine-minimal/issues/145) — Reasoning sandwich
- M en código (perfil por fase + heurística simple), pero doblemente bloqueado:
  **prerequisito #134** (preserved thinking) y **medición vía #139**.
- Además restricción de API: en algunos providers el modo de thinking no cambia a mitad de turno → verificar por adapter.
- Lo difícil no es escribirlo, es verificarlo como hipótesis falsable.

### 23. [#149](https://github.com/phoson-lat/phoson-engine-minimal/issues/149) — Handoff multi-sesión
- L: contrato nuevo de engine (artefacto versionado de handoff), decisión de arquitectura
  (engine vs plugin), persistencia en checkpoint (Postgres), modo de arranque desde handoff.
- Marcado **diferido**: #129 (background agents) es el caso de uso que lo justifica; construirlo antes es escribir un contrato que nadie cruza.

### 24. [#129](https://github.com/phoson-lat/phoson-engine-minimal/issues/129) — Background agents
- **El más grande:** 6 slices, supervisor daemon, decisiones de transporte (socket/HTTP/file-watching),
  proceso (daemon vs setsid), permisos unattended, crash safety, costo agregado.
- Es un producto nuevo dentro del CLI. El slice 1 (resumabilidad desde el último step commitado)
  es S-M y desbloquea la primera piedra de #149.

## Sin "complejidad de código" (se resuelven con decisión o secuencia, no con un PR de feature)

| Issue | Tipo | Nota |
|-------|------|------|
| [#161](https://github.com/phoson-lat/phoson-engine-minimal/issues/161) T-11 ADR | Decisión | El ADR ya está escrito (`IMPROVEMENTS-TUI.md` §B); falta el acuerdo del equipo. Solo se vuelve L si alguien fuerza cambiar de toolkit. |
| [#160](https://github.com/phoson-lat/phoson-engine-minimal/issues/160) T-10 hero | Secuencial | S en sí (un VHS tape), pero el GIF miente si se hace antes que T-1…T-9. |
| [#148](https://github.com/phoson-lat/phoson-engine-minimal/issues/148) tool budget | Medición | Hoy es "instrumentar y mirar": sale gratis con #140. Feature solo si los datos lo justifican. Restricción crítica: no remover tools a mitad de sesión (KV-cache). |

---

## Orden práctico sugerido (quick wins primero)

```
Fase 1 — quick wins (1 día c/u, sin riesgo)
  #155 (Thinking Ns) ✅  #159 (footer) ✅  #138 (bench fix) ✅  #150 (picker)

Fase 2 — sprint look 0 (1-2 PRs densos)
  #151, #152  →  PR "chrome/transcript secos"
  #153, #154  →  PR "reasoning + composer"   (#155 ya va desde Fase 1)

Fase 3 — sprint look 1 + fixes independientes
  #156 (chip+card) ✅  #157 (tool cards) ✅  #158 (system theme) ✅  — resueltos en la rama
  #141 (run budget)                →  engine fix independiente, no mezclar

Fase 4 — la infra del harness (se alimentan entre sí)
  #140 slice 1 (trace-file JSON)  →  #139 (eval set + gate nightly)   [#138 ✅ ya destraba el modelo fijo]
  #161 (acuerdo del ADR, cuando toque)
  #160 (hero tape, recién con look 0+1 merged)
  #162 (palette + ! bash, recién con #154; #156 ya está)

Fase 5 — cadena de reasoning
  #134 (preserved thinking)  →  #145 (sandwich)   [ambos miden contra #139]

Fase 6 — middleware contra el gate
  #142 (doom loop)  #143 (presupuesto en contexto)   [tras #139]

Fase 7 — seguridad sostenida
  #144 Fase 2 (anotaciones MCP, S-M, ya se puede)
  #144 Fases 1+3   [sprint 3, con H-1]
  #146 (docs parity: PR de docs ya; gate en sprint 4)

Fase 8 — sprint 4 / diferido
  #147 (compact tool)  #148 (medir)  #149 (handoff)  #129 (background, 6 slices)
```

### Reglas del orden

1. **Nunca mezclar** T-6 (#156) con el PR de "secar el look": toca permisos.
2. **#138 antes que #139** (bloqueo duro) — **resuelto** ✅; #139 ya no está bloqueado por el modelo fijo.
3. **#140 slice 1 antes que #139**: el gate nightly necesita el trace para clasificar fallas.
4. **#158 (system theme) va último del sprint look 1** porque cambia el default — después de que T-1…T-7 ya se vean bien sobre el purple actual.
5. Todo lo que diga "medido contra H-1" (#142, #143, #145, #147) **no está listo para atacarse** hasta que #139 exista.

---

## Relación con otros documentos

- `IMPROVEMENTS-TUI.md` — los T-* (look + ADR); sus IDs de issue ya están anotados en la tabla.
- `IMPROVEMENTS.md` — los H-* (harness → #138…#149) y el historial I-*.
- Este archivo es **orden de ataque transversal**: mezcla H-* y T-* en una sola cola. Si `IMPROVEMENTS.md` cambia el sprint de algún H-*, se actualiza aquí.
