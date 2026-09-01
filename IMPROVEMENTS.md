# IMPROVEMENTS — phoson-engine-minimal / phoson-cli

> **Origen:** Roadmap activo de resolución de issues abiertos de GitHub para `phoson-engine-minimal` y `phoson-cli`.
>
> **Cómo usar este documento:** Cada ítem corresponde a un issue abierto en GitHub (sección A) o a un plan de trabajo derivado de la revisión externa del harness (sección B, `reporte-harness.md`), con prioridad (P0–P2), estimación de esfuerzo (S/M/L), análisis verificado en código, solución propuesta, criterios de aceptación y una decisión de ataque (Sprint N / diferido / descartado).
>
> **Estado de referencia:** v0.20.0 · 1848 tests passing (38 skipped) · pyright 0 errors · ruff clean.
>
> **Actualización 2026-08-30:** (1) se añadieron los issues abiertos #129 y #134; (2) se incorporó el plan de trabajo del harness (`reporte-harness.md`) **verificado contra el código** — varios supuestos del reporte se corrigieron (ver "Verificación contra código" en B); (3) el historial resuelto se comprimió a una línea por ítem — el detalle completo sigue en `docs/plans/I-NNN.md`, CHANGELOG y git history (el archivo anterior quedó versionado en git).

---

## Tabla resumen

### A. Issues abiertos en GitHub

| ID | Issue | Título | Prioridad | Esfuerzo | Impacto | Estado |
|----|-------|--------|-----------|----------|---------|--------|
| **I-134** | [#134](https://github.com/phoson-lat/phoson-engine-minimal/issues/134) | Preserved thinking: retener razonamiento entre turnos (Qwen3.8 `preserve_thinking`, bloques de thinking de Anthropic) | **P1** | M | 🟠 Medio (consistencia multi-turno en local + prerequisite de H-5) | 🔴 Abierto |
| **I-129** | [#129](https://github.com/phoson-lat/phoson-engine-minimal/issues/129) | Background agents: detachar un run del CLI, dejarlo corriendo y reanudarlo después | **P2** | L (6 slices) | 🟡 Medio (caso de uso; desbloquea `phoson_http` y es el host natural de H-10) | 🔴 Abierto |

### B. Plan de trabajo del harness (de `reporte-harness.md`, verificado en código)

| ID | Título | Prioridad | Esfuerzo | Impacto | Issue | Decisión |
|----|--------|-----------|----------|---------|-------|----------|
| **H-0** | *(nuevo)* Bug de verificación: `bench/` ignora `--model`/`--provider` (env vars inexistente) | **P0** | S | 🔴 Crítico (H-1 no ejecutable sin esto) | [#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138) | ✅ Resuelto (post-v0.20.0) |
| **H-1** | Set de evaluación de agente + gate de no-regresión en CI (nightly) | **P0** | M (revisado ↓ desde L) | 🔴 Crítico (hoy no existe función de fitness) | [#139](https://github.com/phoson-lat/phoson-engine-minimal/issues/139) | Sprint 1 |
| **H-2** | Exportación de trazas: `phoson_plugin_otel` | **P0** | M | 🔴 Alto (precondición de H-1, H-11 y de #129) | [#140](https://github.com/phoson-lat/phoson-engine-minimal/issues/140) | Sprint 1 |
| **H-7** | Presupuesto de wall-clock en modo no-interactivo | **P1** | S | 🟠 Medio (one-shot no tiene Esc; hueco real en CI) | [#141](https://github.com/phoson-lat/phoson-engine-minimal/issues/141) | Sprint 1 *(adelantado desde sprint 2)* |
| **H-3** | Middleware de detección de doom loops | **P1** | S-M | 🟠 Medio (quema de presupuesto en ciclos estériles) | [#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142) | Sprint 2 |
| **H-4** | Inyección de contexto ambiental (presupuesto de turnos/tiempo) | **P1** | S | 🟠 Medio (el agente no prioriza si no sabe qué le queda) | [#143](https://github.com/phoson-lat/phoson-engine-minimal/issues/143) | Sprint 2 |
| **H-6** | Permisos por intención + anotaciones MCP + audit log (3 fases) | **P1** | M-L | 🔴 Alto (granularidad actual no captura el riesgo real) | [#144](https://github.com/phoson-lat/phoson-engine-minimal/issues/144) | Sprint 3 (solo Fase 2 antes) |
| **H-5** | Reasoning effort por fase ("reasoning sandwich") | **P1** | M | 🟠 Medio (perilla global mal aprovechada) | [#145](https://github.com/phoson-lat/phoson-engine-minimal/issues/145) | Sprint 3 (como hipótesis, depende de I-134) |
| **H-8** | Paridad docs↔código en CI, extendida al contexto de agente | **P2** | S | 🟡 Medio (deriva documental = contexto envenenado) | [#146](https://github.com/phoson-lat/phoson-engine-minimal/issues/146) | Sprint 4 (gate); PR de docs en Sprint 0 |
| **H-9** | Compactación controlada por el agente | **P2** | M | 🟡 Medio (hoy es reactiva a umbral) | [#147](https://github.com/phoson-lat/phoson-engine-minimal/issues/147) | Sprint 4 |
| **H-11** | Presupuesto de tools / carga diferida cache-aware | **P2** | M → **analizar primero** | 🟡 Medio (crece sin techo con MCP + plugins) | [#148](https://github.com/phoson-lat/phoson-engine-minimal/issues/148) | Sprint 4 (cero esfuerzo hasta ver datos de H-2) |
| **H-10** | Contrato de handoff para tareas multi-sesión | **P2** | L | 🟡 Medio (engine stateless por run) | [#149](https://github.com/phoson-lat/phoson-engine-minimal/issues/149) | Diferido (I-129 es su caso de uso) |

---

## A. Issues abiertos en GitHub

### I-134 — [Feature #134] Preserved thinking: retener razonamiento entre turnos
* **Estado:** 🔴 **Abierto** (creado 2026-08-30, label `enhancement`)
* **Área:** `phoson_llm/schemas/inputs.py`, `phoson_agent/_loop.py`, `phoson_llm/chats/{_openai_compatible,anthropic,vllm,lmstudio,ollama}.py`
* **Prioridad:** **P1** · **Esfuerzo:** M · **Impacto:** 🟠 Medio
* **Resumen:** El engine descarta el reasoning tras streamearlo a la UI. Con Qwen3.8 en vLLM/LM Studio (modelo local realista para este engine), `preserve_thinking` queda efectivamente roto porque el cliente no devuelve `reasoning_content` en los mensajes assistant históricos; y el extended thinking + tool use de Anthropic (que **requiere** devolver los bloques de thinking previos con firma) no puede funcionar hoy.
* **Estado verificado en código (2026-08-30):**
  1. `ReasoningDoneEvent` existe (`phoson_llm/schemas/outputs.py:70`) y `_openai_compatible.py:527` lo emite — la señal ya está disponible.
  2. `phoson_agent/_loop.py::_build_assistant_message()` construye el mensaje assistant **solo con** `TextBlock` + `ToolUseBlock` — el reasoning capturado no se adjunta al historial. Confirmado: el gap es real y el punto de arreglo está identificado.
  3. El CLI ya persiste reasoning por nodo en `node.metadata["reasoning"]` (`_persist_run_reasoning`) y el summarizer lo pliega al compactar (E1) — hay material para reusar, nada se ha enviado al modelo.
* **Propuesta (según el issue, validada):** schema `reasoning: str | None` (+ `reasoning_signature` para Anthropic) en `Message`; `AgentLoop` captura y adjunta en ambos paths (final y tool calls); el adapter OpenAI-compat emite `reasoning_content` y acepta `preserve_thinking` por request; Anthropic reconstruye bloques `thinking`/`redacted_thinking`; `ModelConfig.preserve_thinking: bool | None = None` (pattern "ignore if unsupported" como `session_id`); cap policy documentado; `reasoning_tokens` en `TokenUsage` donde el provider lo reporte.
* **Conexión con el plan del harness:** es el **prerequisite de H-5** — el propio reporte-harness advierte que "los bloques de thinking deben preservarse al devolver resultados de tool (omitirlos rompe silenciosamente el razonamiento multi-paso)" antes de poder variar el effort por fase. Atacar I-134 primero convierte a H-5 de feature frágil en hipótesis medible.
* **Criterio de listo (del issue):** multi-turno contra vLLM/Qwen3.8 devuelve `reasoning_content`; Anthropic con `thinking_budget` + tool use no falla entre iteraciones; sesiones persisten y reanudan el reasoning; tests con streams fakeados + ruff/pyright/pytest limpios.
* **Ataque:** **Sprint 3, antes de H-5.** Estimación M: el punto de captura ya existe (`ReasoningDoneEvent`), falta schema + 2 adapters + persistencia + cap policy.

### I-129 — [Feature #129] Background agents: detachar un run, dejarlo corriendo y reanudar
* **Estado:** 🔴 **Abierto** (creado 2026-08-30, label `enhancement`)
* **Área:** nuevo paquete `phoson_plugin_background/`, `phoson_agent/sessions/`, `phoson_cli/` (subcomando `bg`)
* **Prioridad:** **P2** · **Esfuerzo:** L (6 slices independientes y mergeables) · **Impacto:** 🟡 Medio
* **Resumen:** Correr un agente largo (refactor, fix-the-tests, migración) amarra al usuario al terminal: cerrar el CLI mata el run. Faltan: detachar, correr varios en paralelo, inspeccionar después, reanudar. El issue lo propone como **plugin oficial** (`phoson_plugin_background`, entry point `phoson.plugins`) para mantener el core minimal, con 6 slices: (1) hardening de resumabilidad en `phoson_agent.sessions`, (2) esqueleto del plugin + `phoson-cli bg list` sobre run logs locales, (3) supervisor daemon + detach (`/bg`, `--bg`), (4) attach/live-follow + re-attach al REPL vía `/resume`, (5) stop/cancel + política de permisos unattended, (6) docs + ejemplo.
* **Estado verificado en código (2026-08-30):** los bloques ya existen — `JsonlStorage` con writes atómicos, `/resume` + picker de sesiones en TUI y clásico, contrato `Plugin` con `aclose()` (I-110) y el patrón de plugins oficiales (I-126 demostró que un daemon-ish plugin con persistencia "el disco es la verdad, las tareas son caché" cabe en el contrato sin tocar `phoson_agent`).
* **Conexión con el plan del harness:**
  - Es el **caso de uso concreto de H-10** (contrato de handoff multi-sesión): un background run que supera la ventana necesita exactamente el artefacto de handoff que H-10 propone.
  - **H-7 (wall-clock budget) es un requisito de seguridad de este feature:** un run unattended sin Esc **necesita** un presupuesto de run; el slice 5 (política de permisos unattended) debe reusar el fail-closed ya existente de `permissions.py`.
  - El slice 2 (`bg list` con estado/costo/tokens) se alimenta gratis de `RunStep`/`UsageEvent` — y de H-2 cuando exista.
  - I-126 (monitores, v0.19.0) confirmó que el host del monitor natural de este supervisor es un background run.
* **Preguntas de diseño abiertas (del issue):** transporte al supervisor (socket/HTTP/file-watching — decide también el futuro de `phoson_http`), proceso daemon vs `setsid`, permisos unattended, tope de concurrentes, costo agregado.
* **Criterio de listo (por slice):** ver body del issue. El mínimo viable útil = slice 1 + 2 (resumabilidad garantizada + `bg list` sin daemon): ya da "se me murió, ¿dónde quedó?" sin un solo daemon.
* **Ataque:** **Sprint 4, slice 1 + 2** (S-M). Slices 3–6 cuando el slice 2 esté en manos de alguien y justifique el daemon. **No tocar H-10 hasta que I-129 pida handoff real** (hoy es speculativo; el issue lo hace concreto).

---

## B. Plan de trabajo del harness (de `reporte-harness.md`)

> **Nota de origen:** la revisión es externa y se hizo sin acceso al código. Cada ítem debajo incluye una línea **"Verificado en código"** con lo que cambió respecto al reporte. Tesis que se sostiene: **el gate de no-regresión (H-1) es lo que convierte todo lo demás de opinión en ingeniería** — sin H-1+H-2, los otros nueve ítems son apuestas sin contraparte.
>
> **Contrapeso honesto (del reporte, mantener):** el payoff del harness engineering pega fuerte en el tier medio/local (vLLM/Ollama/LM Studio, modelo fijo y más débil) y se diluye contra frontier vía OpenRouter; y cada pieza nueva existe porque asume que el modelo no puede algo — **marcar la asunción que justifica cada pieza para poder retirarla cuando expire.**

### Verificación contra código (corregido del reporte)

| Afirmación del reporte | Veredicto |
|---|---|
| "El repo map del README lista `bench/` pero no aparece en el raíz" (nota H-1) | ❌ **Falso.** `bench/` existe: `run_bench.py` + 4 tasks con checkers deterministas, corriendo el one-shot en workspaces aislados. H-1 **no parte de cero** — parte de un embrión funcional. |
| "No hay forma de saber si un cambio de harness mejoró o empeoró" | ✅ Ciertamente no hay **gate**: `bench/` existe pero no corre en CI (nada en `.github/workflows/` lo invoca) y no hay baseline registrada. |
| "El script de paridad de I-115 existe como precedente" (H-8) | ⚠️ **A medias.** `docs/plans/I-115.md` documenta el check 35/35, pero **el script no existe en `scripts/`** — fue ad-hoc. Hay que construirlo. |
| "Permiso por tool + allow-patterns, fail-closed en no-interactivo" (H-6) | ✅ Exacto (`phoson_agent/permissions.py`, middleware `on_before_tool`). Sin audit log; el plugin MCP **no** consume `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`. |
| "Controls de terminación: solo `max_turns` y `subagent_timeout_seconds`" (H-3) | ✅ Correcto. Sin detección de loops (grep negativo). |
| "El agente no sabe cuántos turnos le quedan" (H-4) | ✅ Correcto. Nada inyecta presupuesto de turnos/tiempo al contexto (solo `date` disponible vía bash). |
| "One-shot no tiene Esc" (H-7) | ✅ Correcto. `_run_oneshot` no tiene tope de wall-clock; I-127 dejó el timeout de bash sin tope por decisión del owner (válida para interactivo). |
| "Reasoning effort es global de sesión" (H-5) | ✅ Correcto. `/reasoning-effort` es un solo valor en `PhosonConfig.reasoning_effort`. |
| "v0.18.0 · ~1600 tests" (estado de referencia) | ⚠️ Desfasado. Hoy es **v0.19.0 · 1819 passed (38 skipped)**; I-126 (monitores) ya está shipped, lo que confirma la distinción del reporte "monitores ≠ handoff". |

### Bug encontrado durante la verificación (motiva H-0)

`bench/run_bench.py` inyecta `PHOSON_MODEL_OVERRIDE` / `PHOSON_PROVIDER_OVERRIDE` en el env del subprocess, pero `phoson_cli/config.py` solo lee `PHOSON_MODEL` / `PHOSON_PROVIDER`. **Los flags `--model`/`--provider` del bench son ignorados silenciosamente** (y su `bench/README.md` documenta el override que no funciona). Consecuencia: "fijar el modelo pineado" — punto 3 del H-1 — es incumplible hoy, y cualquier comparación entre modelos hecha con este runner es inválida.

---

### H-0 — [Bug #138] Fix del runner de bench: model/provider override no funciona ✅ *resuelto (post-v0.20.0)*
* **Prioridad:** **P0** · **Esfuerzo:** S · **Impacto:** 🔴 Crítico (bloquea el criterio de listo del H-1) · **Issue:** [#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138)
* **Área:** `bench/run_bench.py`, `phoson_cli/config.py` (o solo el bench)
* **Fix aplicado (variante de la opción b, aún más simple):** la opción "recomendada" (b) no era cero-cambios en realidad — `config.py` hardcodea `~/.phoson/config.toml` y no respeta `PHOSON_CONFIG`/`XDG_CONFIG_HOME`. Lo mínimo fue inyectar las env **existentes** `PHOSON_MODEL`/`PHOSON_PROVIDER` (que el CLI ya resuelve con prioridad env → config.toml → default), **popeando** antes las heredadas del shell del dev para que una `PHOSON_MODEL` propia no re-apunte una baseline. `_build_env()` + `_results_payload()` en `bench/run_bench.py` (JSON con `model`/`provider`/`commit`), `bench/README.md` corregido y 5 tests en `tests/test_bench_runner.py`. Verificado end-to-end: `--model openai/gpt-4o-mini --provider openrouter` corrió un LLM real y el JSON registró los valores.
* **Problema:** ver "Bug encontrado" arriba.
* **Solución (elegir una, preferir la más simple):**
  - (a) Hacer que `config.py` lea los `*_OVERRIDE` (o un par de env nuevas, p. ej. `PHOSON_BENCH_MODEL`) — cambia el config real, requiere decisión.
  - (b) En el bench, escribir un `config.toml` efímero en el workspace y apuntar `PHOSON_CONFIG`/`XDG_CONFIG_HOME` al workspace (el runner ya crea el workspace) — **cero cambios en el CLI**, el modelo se fija en disco y queda visible en el JSON de resultados. Recomendado.
  - (c) Agregar un flag `--model`/`--provider` al one-shot del CLI.
  - Independientemente: el JSON de resultados (`bench/results/*.json`) debe incluir modelo+provider+commit para que la baseline sea auditada.
* **Criterio de listo:**
  - `run_bench.py --model X --provider Y` corre **de hecho** con X/Y (test que inspeccione el JSON o una task que reporte su modelo).
  - `bench/README.md` corregido; el modelo queda registrado en cada JSON de resultados.
* **Ataque:** **Sprint 0** (1 día, con +2–3 tasks nuevas para tener 6–7 antes de la baseline).

### H-1 — [Feature #139] Set de evaluación de agente + gate de no-regresión en CI (nightly)
* **Prioridad:** **P0** · **Esfuerzo:** **M** (revisado hacia abajo desde L: el runner y 4 tasks ya existen) · **Impacto:** 🔴 Crítico · **Issue:** [#139](https://github.com/phoson-lat/phoson-engine-minimal/issues/139)
* **Área:** `bench/` (existente), `.github/workflows/` (nuevo job)
* **Problema:** los ~1800 tests miden corrección de software, no calidad de agente. No hay un número que responda "¿este harness resuelve más tareas que el de la semana pasada?". I-127 (timeout sin tope), I-128 (evento nuevo en el stream), I-91 (gate de compactación) pudieron mover la tasa de éxito sin que hubiera forma de saberlo. Evidencia citada: la auto-atribución de cambios de harness es ~5× mejor que el azar prediciendo qué tareas **arreglará** una edición, y **apenas mejor que el azar prediciendo cuáles romperá** — sin gate, cada merge de harness es una apuesta ciega para regresiones.
* **Estado verificado en código:** el embrión existe (4 tasks: `create-json-config`, `csv-stats`, `fix-failing-script`, `fix-import-error`) con checkers deterministas y workspaces aislados. Faltan: modelo fijo y verificable (H-0), densidad (15–25 tasks por discriminación), baseline + varianza medida, y el gate en CI.
* **Solución propuesta:**
  1. **No Terminal-Bench completo** (89 tasks Docker): demasiado pesado para CI de un repo personal. Usar el runner propio (ya alineado con el espíritu de Terminal-Bench) y seleccionar 15–25 tasks por **discriminación** (pass parcial entre candidatos), no por dificultad.
  2. **Modelo fijo para el nightly: local** (vLLM/Ollama/LM Studio — los 3 adapters ya existen). CI gratis, sin API key, reproducible, y es el régimen donde el harness paga completo (modelo fijo y más débil). OpenRouter queda para spot-checks manuales con el `--model` corregido de H-0.
  3. **Split honesto:** subset de evolución (contra el que se itera) + subset held-out reportado aparte.
  4. **Gate en CI:** job **nightly** (no por PR — es caro) que corre el set ≥3 veces (varianza medida) y falla si la tasa cae por debajo de la baseline menos el ruido. Los empates se rechazan.
  5. Baseline registrada en el repo: tasa, modelo, fecha, commit.
* **Criterio de listo:**
  - `bench/` con 15–25 tasks (6–7 ya existen post-H-0), verificadores deterministas, un comando para correrlas.
  - Baseline registrada + ≥3 corridas para medir el ruido.
  - Workflow nightly que publica el resultado y falla bajo baseline − varianza.
  - Split held-out documentado y nunca usado para iterar.
* **Ataque:** **Sprint 1.** Cada PR de harness a partir de entonces **declara un contrato falsable** (qué tareas predice que arregla / pone en riesgo), verificado en la corrida siguiente.

### H-2 — [Feature #140] Exportación de trazas: `phoson_plugin_otel`
* **Prioridad:** **P0** · **Esfuerzo:** M · **Impacto:** 🔴 Alto · **Issue:** [#140](https://github.com/phoson-lat/phoson-engine-minimal/issues/140)
* **Área:** nuevo paquete `phoson_plugin_otel/` (convenciones de `phoson_plugin_*`: `_plugin.py`, `plugin = MyPlugin()`, README, entrada en "Bundled plugins" de `docs/plugins.md`)
* **Problema:** sin trazas exportables no se comparan corridas (precondición de H-1 a escala) ni se clasifican modos de falla. El README lo lista como "idea para contribuciones"; debe subir a trabajo propio.
* **Estado verificado en código:** la parte difícil ya está hecha — `LLMEvent` con 10 tipos, eventos de agente (`AgentToolComposingEvent`, `AgentToolStart/Done`, `AgentStepDoneEvent`, `AgentDone/Error`), `RunStep` con costo USD por step. **Solo falta el sink.** (Confirmado: nada OTel en `phoson_agent`/`phoson_cli`/`phoson_llm`.)
* **Solución propuesta:**
  - Plugin que mapee el stream de eventos a spans jerárquicos `run → step → llm_call` y `run → step → tool_call`.
  - Atributos por span: modelo, provider, tokens (input/output/cached/reasoning), costo USD, latencia, tool + resultado (ok/error/timeout/denied-by-permission), decisión de permiso.
  - Correlación por `session_id` + `run_id` + `step_index`.
  - Exportador OTLP estándar (Langfuse/Honeycomb/LGTM: el consumidor apunta a donde quiera). Cero acoplamiento a vendor.
* **Qué desbloquea:** H-1 (comparar corridas); atribución de fallas por capa (localizar antes de arreglar); adopción externa en producción; **el "medir primero" de H-11** (tokens de definiciones de tools por configuración, gratis como atributo de span); **el slice 2 de I-129** (`bg list` con costo agregado).
* **Criterio de listo:**
  - Un run genera un trace completo en un colector OTLP local con la jerarquía visible.
  - Tokens/costo por span cuadran con `/cost` y `/tokens`.
  - Los 4 plugins oficiales existentes funcionan sin cambios.
  - Overhead medido (principio #3: benchmark del plugin activo vs inactivo).
* **Ataque:** **Sprint 1**, en paralelo con H-1 (el sink puede escribir JSON local antes que OTLP real — un "trace file sink" de 1 día ya basta para el gate nightly del H-1; OTLP se completa después sin reescribir el mapeo).

### H-7 — [Feature #141] Presupuesto de wall-clock en modo no-interactivo
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio · **Issue:** [#141](https://github.com/phoson-lat/phoson-engine-minimal/issues/141)
* **Área:** `phoson_cli/__main__.py`, `phoson_cli/_run_oneshot`, `docs/cli/`
* **Problema:** I-127 quitó el tope del timeout de bash — bien razonado **para interactivo** (el escape es Esc). **El one-shot no tiene Esc**: `phoson-cli "task"` en un pipeline de CI, con un comando colgado y un timeout que el agente puede fijar a lo que quiera, corre hasta que algo externo lo mate. Es un hueco de categoría, no un desacuerdo con I-127.
* **Estado verificado en código:** `_run_oneshot()` no tiene tope de run; ningún `PHOSON_*BUDGET*` existe (grep negativo).
* **Solución propuesta:**
  - Presupuesto a nivel de **run**, solo en no-interactivo (one-shot / stdin piped): `PHOSON_RUN_BUDGET_SECONDS`, default p. ej. 600s, `0` = sin límite explícito.
  - Al agotarse: terminar limpio, exit code ≠ 0, mensaje identificable (no traceback), cerrando plugins y sesiones MCP como ya hace el teardown existente.
  - No tocar el modo interactivo: la decisión del owner se mantiene donde aplica.
  - Se conecta con H-4 (el presupuesto restante es lo que conviene inyectar) y con I-129 (un run unattended **necesita** este tope).
* **Criterio de listo:**
  - Test: one-shot con tool colgada termina en el presupuesto con exit code 1 y mensaje limpio.
  - Test: modo interactivo inalterado (sin tope, Esc sigue siendo el escape).
  - Documentado en `docs/cli/` + tabla de env vars del README.
* **Ataque:** **Sprint 1** (adelantado desde el sprint 2 del reporte: es aislado, testable sin LLM, y cierra un hueco de seguridad real en CI).

### H-3 — [Feature #142] Middleware de detección de doom loops
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio · **Issue:** [#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142)
* **Área:** `phoson_agent/middleware.py` (nuevo middleware), config
* **Problema:** los controles de terminación son `max_iterations` y `subagent_timeout_seconds`; ninguno detecta "el agente corrió el mismo comando fallido cinco veces". El run consume su presupuesto en un ciclo estéril y termina por agotamiento, no por decisión. La detección de loops fue ingrediente explícito del salto de LangChain en Terminal-Bench 2.0.
* **Estado verificado en código:** hooks `on_before_tool`/`on_after_tool` ya existen en `AgentMiddleware` — el middleware enchufa sin tocar el loop. Grep negativo: sin detección de repetición.
* **Solución propuesta:**
  - Historial per-run de `hash(tool_name, args_normalizados)`; al detectar N repeticiones (default 3) de la misma tupla **con el mismo resultado de error**, actuar.
  - Dos modos configurables: **inyectar** observación al contexto ("ya intentaste esto 3 veces; prueba otra cosa") — default, menos invasivo; o **cortar** el run con error estructurado.
  - Normalización de args: `bash("pytest -q")` ≡ `bash("pytest -q ")` (whitespace, orden de keys).
  - `PHOSON_LOOP_DETECT_N`, `0` = off.
* **Criterio de listo:**
  - Test: stream simulado que repite N veces la misma tool call fallida dispara la acción.
  - Test: repeticiones legítimas (args distintos, o misma tupla con resultado distinto) **no** disparan.
  - Medido contra el set de H-1: no baja la tasa.
* **Ataque:** **Sprint 2** (después de H-1: su criterio de listo "no baja la tasa" es incumplible antes).

### H-4 — [Feature #143] Inyección de contexto ambiental (presupuesto de turnos/tiempo)
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio · **Issue:** [#143](https://github.com/phoson-lat/phoson-engine-minimal/issues/143)
* **Área:** `phoson_agent/middleware.py` (nuevo middleware `before_model`), reutiliza el calculo del gate I-91
* **Problema:** el agente no sabe cuántos turnos le quedan ni cuánto tiempo lleva → no prioriza: gasta igual el turno 2 que el 18 de 20. Evidencia del reporte: la conciencia temporal es ortogonal a la capacidad de razonamiento.
* **Estado verificado en código:** nada inyecta presupuesto (grep negativo). El hook `on_before_llm` existe. El gate de I-91 ya calcula la fracción de ventana consumida — reutilizable.
* **Solución propuesta:**
  - Middleware `on_before_llm` que añada un bloque corto y estable **al final** del contexto: `step 12/20`, tiempo transcurrido/restante (si hay presupuesto, ver H-7), fracción de ventana consumida.
  - **Restricción de diseño:** al final, nunca al inicio — el prefijo debe permanecer estable para el prompt caching (50–90% de ahorro reportado en sesiones largas).
  - Mapa de directorio solo si CWD es repo y bajo un tope de tamaño; degradar a nada si excede.
* **Criterio de listo:**
  - Test que inspecciona el payload: el bloque aparece al final.
  - Test de regresión de caché: fracción de tokens cacheados no cae vs baseline.
  - Medido contra H-1.
* **Ataque:** **Sprint 2**, junto con H-3 (mismo patrón de middleware barato sobre hooks existentes, ambos testables con streams simulados sin LLM).

### H-5 — [Feature #145] Reasoning effort por fase ("reasoning sandwich")
* **Prioridad:** **P1** · **Esfuerzo:** M · **Impacto:** 🟠 Medio · **Issue:** [#145](https://github.com/phoson-lat/phoson-engine-minimal/issues/145)
* **Área:** `phoson_agent/`, `phoson_cli/commands.py`, `phoson_llm/chats/`
* **Problema:** `/reasoning-effort` es un control global de sesión; pero razonar no vale lo mismo en todas las fases (planear/verificar sí; ejecutar un `cat` mecánico, no). El patrón de LangChain (máximo thinking en planning/verification, mínimo en ejecución) fue uno de sus tres pasos de progresión.
* **Estado verificado en código:** `/reasoning-effort` es un solo valor en `PhosonConfig.reasoning_effort`, forwardeado como-is a OpenAI-compat. Sin noción de fase.
* **Dependencia nueva (de la verificación):** **I-134 primero.** El propio reporte advierte que los bloques de thinking deben preservarse al devolver tool results — hoy no se preservan en absoluto (`_build_assistant_message` los descarta). Hacer sandwich sobre un historial que pierde el reasoning es construir sobre arena.
* **Solución propuesta:**
  - Perfil por fase configurable, default conservador: `planning: high / execution: low / verification: high`; detección heurística simple (primer step = planning; step tras tool de test/build fallida = verification; resto = execution).
  - El override global del usuario gana siempre.
  - Verificar comportamiento por adapter antes de generalizar (algunos no cambian thinking a mitad de turno).
* **Criterio de listo:** perfil en `config.toml` documentado; bloques de thinking preservados entre iteraciones (I-134); medido contra H-1 reportando **tasa y costo** (este cambio mueve ambos).
* **Ataque:** **Sprint 3, como hipótesis falsable** (no como feature a ciegas): se propone, se mide contra el set, se queda o se retira según el delta.

### H-6 — [Feature #144] Permisos por intención + anotaciones MCP + audit log
* **Prioridad:** **P1** · **Esfuerzo:** M-L · **Impacto:** 🔴 Alto · **Issue:** [#144](https://github.com/phoson-lat/phoson-engine-minimal/issues/144)
* **Área:** `phoson_agent/permissions.py`, `phoson_cli/permissions_store.py`, `phoson_plugin_mcp/`
* **Problema:** allow/ask/deny por nombre de tool + allow-patterns (regex) es la granularidad equivocada: `allow bash` no distingue `ls` de `rm -rf`; las anotaciones MCP no se consumen; no hay audit log; y con MCP (datos privados + contenido no confiable + comunicación externa) el riesgo emerge de **combinaciones** de tools, no de una sola. El consenso 2026: en un agente con tools, la inyección de prompts es un problema de **autorización**, no de contenido.
* **Estado verificado en código:** `permissions.py` = middleware declarativo con `on_before_tool`, levels + fnmatch patterns, fail-closed en no-interactivo, denials devueltos al modelo como resultado de tool (buen diseño, se conserva). Sin audit log, sin anotaciones MCP (grep negativo en `phoson_plugin_mcp`).
* **Solución propuesta (3 fases, en orden):**
  1. **Taxonomía de intención:** política contra categorías derivadas de tool + args parseados (`filesystem_read/write/delete`, `network_outbound`, `process_spawn`, `lang_exec`), no contra nombres de comando. Compatibilidad con `permissions.json` actual durante una versión.
  2. **Anotaciones MCP:** consumir `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` como **señal** (no contrato) en la decisión de permiso; sin anotaciones → default seguro. Extiende I-100.
  3. **Audit log + anti-fatiga:** registro estructurado de cada decisión (exportable vía H-2); "negar y continuar" (denegación como observación, el run sigue); quitar mensajes del assistant del contexto de cualquier clasificador.
* **Criterio de listo (del reporte):** una política por intenciones bloquea `bash("rm -rf /")` y permite `bash("ls")` sin listar comandos; anotaciones MCP alimentan la decisión; cada decisión deja registro; migración de `permissions.json`; suite adversarial (instrucciones inyectadas en salida de tool) donde ninguna acción fuera de política se ejecuta.
* **Ataque:** **Sprint 3, solo la Fase 2 como slice pequeño** (es solo leer hints que ya vienen en el protocolo y mapearlos a señal de permiso — S-M, sin migración). **Fases 1 y 3 después de H-1** (no se puede medir regresión de permisos sin el set) y solo si la suite adversarial se construye de verdad. Es el ítem más grande y el más sensible: no atacarlo como bloque.

### H-8 — [Feature #146] Paridad docs↔código en CI, extendida al contexto de agente
* **Prioridad:** **P2** · **Esfuerzo:** S · **Impacto:** 🟡 Medio · **Issue:** [#146](https://github.com/phoson-lat/phoson-engine-minimal/issues/146)
* **Área:** `.github/workflows/ci.yml`, `scripts/`, `TODO.md`
* **Problema:** deriva documental detectable hoy — y `AGENTS.md` se inyecta al system prompt, así que un doc desfasado no es mala documentación: es **contexto envenenado** para el agente que lee el repo.
* **Estado verificado en código (2026-08-30):**
  - ✅ Deriva real 1: `TODO.md:74` — "Textual (full-screen TUI) explícitamente rechazado" vs `README.md` TUI full-screen como default.
  - ✅ Deriva real 2: `TODO.md:107` — permisos por tool como "P3 sin empezar (safe_mode all-or-nothing)" vs shipped (I-110/A1).
  - ❌ El caso 3 del reporte (`bench/` en README pero no en el repo) es **falso positivo**: `bench/` existe.
  - ⚠️ El "precedente" de I-115 (script de paridad 35/35) **no existe en `scripts/`** — fue ad-hoc; hay que construirlo.
* **Solución propuesta:**
  - PR de docs inmediato: corregir las 2 derivas de `TODO.md` (marcando el header "índice histórico — fuente de verdad: `IMPROVEMENTS.md`" más visible).
  - Script de paridad en `scripts/check_docs_parity.py` (comandos slash vs `COMMAND_SPECS`, flags vs `_USAGE`, env vars, bundled plugins) → job de CI por PR.
  - Tope de tamaño para `AGENTS.md` con warning no bloqueante.
* **Criterio de listo:** CI falla ante desync comando/flag/env/plugin; las 2 derivas resueltas; tope de `AGENTS.md` documentado.
* **Ataque:** **PR de docs en Sprint 0** (minutos); **gate de CI en Sprint 4** (requiere el script, que es S-M).

### H-9 — [Feature #147] Compactación controlada por el agente
* **Prioridad:** **P2** · **Esfuerzo:** M · **Impacto:** 🟡 Medio · **Issue:** [#147](https://github.com/phoson-lat/phoson-engine-minimal/issues/147)
* **Área:** `phoson_agent/plugins/summarizer.py`, tool nueva
* **Problema:** I-91 resuelve el bug (gate conservador + rescate 400), pero la compactación sigue siendo reactiva a umbral: puede dispararse a mitad de subtarea e interrumpir el razonamiento en vuelo. El movimiento 2026: una tool que el agente llama cuando le conviene estratégicamente.
* **Advertencia incorporada (del reporte):** dejar que un LLM reescriba monolíticamente su contexto lo erosiona (caso documentado: 18,282 → 122 tokens, accuracy 66.7 → 57.1). Mantener el handoff **estructurado** existente; nunca reescritura libre.
* **Solución propuesta:** tool `compact_context()` (permiso propio, default allow) + umbral automático como red de seguridad; documentar en el system prompt cuándo llamarla y qué sobrevive a la compactación (regla: nunca depender de la compactación para reglas críticas — esas van a `AGENTS.md`/system prompt).
* **Criterio de listo:** la tool es llamable y su efecto es idéntico al de `/compact`; el umbral sigue funcionando; `docs/cli/compaction.md` documenta qué sobrevive y qué se pierde; medido contra H-1 en las tareas más largas del set.
* **Ataque:** **Sprint 4.** Sin el set de H-1 su criterio de medición no existe; y su impacto (🟡) no justifica adelantarlo.

### H-10 — [Feature #149] Contrato de handoff para tareas multi-sesión
* **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟡 Medio · **Issue:** [#149](https://github.com/phoson-lat/phoson-engine-minimal/issues/149)
* **Área:** `phoson_agent/` (contrato + serialización), `phoson_plugin_checkpoint/` (persistencia)
* **Problema:** el engine es stateless por run; "despiértame cuando pase X" (I-126, hecho) no es "sigue donde te quedaste" (tarea que excede una ventana). Falta el **artefacto de handoff versionado**: estado, decisiones, qué falta, qué falló, punto de reanudación — persistido fuera del historial para sobrevivir a compactación.
* **Estado verificado en código:** la mitad de la infra existe (`ConversationTree` + `phoson_plugin_checkpoint` con Postgres real). I-126 (v0.19.0) confirmó que el patrón "disco es la verdad, tasks son caché" cabe en el contrato `Plugin`.
* **Decisión de ataque (revisada por la verificación):** **diferido.** El reporte lo propone por roadmap; hoy no hay caso de uso dominante que lo exija. **I-129 (background agents) es ese caso de uso** — y su slice 1 (resumabilidad desde el último step commitado) es la primera piedra. Atacar H-10 como speculación es construir un contrato que nadie cruza; atacarlo cuando I-129 slice 4 (re-attach al REPL) lo pida, es escribirlo contra un flujo real. Si se hace: contrato en `phoson_agent`, persistencia en el checkpoint plugin (mismo criterio de separación de I-110).
* **Criterio de listo (cuando se ataque):** una tarea que excede la ventana se completa en ≥2 runs con progreso verificable; el handoff sobrevive a reinicio de proceso; test de integración Postgres (skip-si-no-hay-servicio).

### H-11 — [Feature #148] Presupuesto de tools / carga diferida cache-aware
* **Prioridad:** **P2** · **Esfuerzo:** M → **analizar primero** · **Impacto:** 🟡 Medio · **Issue:** [#148](https://github.com/phoson-lat/phoson-engine-minimal/issues/148)
* **Área:** `phoson_agent/`, `phoson_plugin_mcp/`
* **Problema:** con 20+ providers, MCP, plugins, sub-agentes y skills, las definiciones de tools crecen sin techo y consumen contexto en **cada** request. Referencia del reporte: sistemas internos con 134K tokens solo en definiciones; descubrimiento bajo demanda los bajó 77K → 8.7K.
* **Restricción de diseño crítica (del reporte, aceptada):** **no remover tools dinámicamente a mitad de sesión** — invalidan el KV-cache del prefijo y confunden al modelo (acciones referencian tools ya no definidas). Si se implementa, enmascarar sin remover, con prefijo estable.
* **Decisión de ataque (revisada por la verificación):** **cero esfuerzo separado hasta que H-2 exista** — el "medir primero" del propio ítem sale gratis como atributo de span (tokens de definiciones por configuración: sin MCP / con 2 MCP / con MCP+plugins+skills). I-100 (toggle manual, shipped) ya es la versión artesanal y ayuda mientras tanto.
* **Criterio de listo (cuando se ataque):** reporte de tokens de definiciones en 3 configuraciones; si se implementa, fracción de tokens cacheados **no cae** (test de regresión); precisión de recuperación medida sobre el catálogo real (la referencia independiente mide 56–64% — la feature ahorra contexto, no garantiza encontrar la tool).
* **Ataque:** **Sprint 4, como análisis de datos**, no como feature.

---

## C. Roadmap de ataque (propuesto)

```
Sprint 0 — Hacer ejecutable (esta semana)
├── H-0   Fix del bench: --model/--provider real + JSON auditado      P0 · S
├── H-8a  PR de docs: 2 derivas de TODO.md + header histórico visible  P2 · S
└──       +2–3 tasks nuevas en bench/ (para 6–7 pre-baseline)          P0 · S

Sprint 1 — Medir antes de optimizar (habilita todo lo demás)
├── H-1   Set de evaluación (15–25 tasks, modelo LOCAL fijo) + baseline + gate nightly   P0 · M
├── H-2   phoson_plugin_otel (puede arrancar con trace-file sink; OTLP completo después)  P0 · M
└── H-7   Wall-clock budget en no-interactivo (PHOSON_RUN_BUDGET_SECONDS)                P1 · S

Sprint 2 — Ganancias baratas sobre middleware existente (testables sin LLM)
├── H-3   Detección de doom loops (inyectar por default, N=3)         P1 · S-M
└── H-4   Contexto ambiental al final del contexto (step N/M, tiempo, ventana)  P1 · S

Sprint 3 — Razonamiento y control (todo medido contra H-1)
├── I-134 Preserved thinking (schema + loop + adapters + cap policy)  P1 · M
├── H-5   Reasoning effort por fase — hipótesis falsable, se queda o se retira por delta  P1 · M
└── H-6b  Fase 2: anotaciones MCP como señal de permiso (slice sin migración)  P1 · S-M

Sprint 4 — Higiene, horizonte largo y casos de uso concretos
├── H-8b  Gate de paridad docs↔código en CI (script check_docs_parity.py)  P2 · S-M
├── H-9   Compactación controlada por el agente (tool + umbral como fallback)  P2 · M
├── H-11  Análisis de tokens de definiciones con datos de H-2 (feature solo si el dato lo pide)  P2 · M
└── I-129 Slices 1+2: resumabilidad garantizada + `bg list` sin daemon  P2 · M

Diferido
├── H-10  Handoff multi-sesión — cuando I-129 slice 4 lo pida contra un flujo real
├── H-6a/c Fase 1 (taxonomía de intención) + Fase 3 (audit log anti-fatiga) — tras H-1, con suite adversarial
└── H-11  Implementación de descubrimiento bajo demanda — solo si el análisis lo justifica
```

**Lo que no se ataca y por qué (resumen de decisiones):**

1. **Terminal-Bench completo (89 tasks Docker)** como sustrato del H-1: demasiado pesado para CI de este repo; el runner propio (ya alineado con su espíritu) con 15–25 tasks por discriminación alcanza, y el modelo local gratis en CI es la ganancia.
2. **H-10 antes de I-129:** un contrato de handoff sin caso de uso que lo cruce es speculación; I-129 slice 1+2 ya da el 80% del valor ("se me murió, ¿dónde quedó?") sin el contrato.
3. **H-6 completo como bloque:** el ítem más grande (M-L) y el más sensible (permisos ya shipped). Solo la Fase 2 entra antes; 1 y 3 entran con H-1 mediendo y suite adversarial de verdad.
4. **H-11 como feature:** el propio ítem dice "medir primero"; H-2 produce esa medición gratis.
5. **Esperar frontier para justificar el gate:** el payoff del harness paga completo en local (vLLM/Ollama/LM Studio, modelo fijo y débil — los 3 adapters ya existen). El nightly corre local; OpenRouter es spot-check manual.
6. **Cada pieza nueva lleva su asunción documentada** (del reporte): si el modelo base deja de necesitarla, se retira.

**Práctica transversal desde Sprint 1:** cada PR de harness declara un **contrato falsable** — qué tareas del set predice que arregla y cuáles pone en riesgo — verificado en la corrida siguiente del gate.

---

## D. Resueltos (historial comprimido)

> Detalle completo: `docs/plans/I-NNN.md` + CHANGELOG + git history. Este archivo ya no duplica esos detalles.

| ID | Issue | Resumen | Versión |
|----|-------|---------|---------|
| **I-128** | [#128](https://github.com/phoson-lat/phoson-engine-minimal/issues/128) | `AgentToolComposingEvent` (throttle ~250 ms) para feedback en UI mientras el modelo compone la tool call | v0.16.0 |
| **I-119** | [#119](https://github.com/phoson-lat/phoson-engine-minimal/issues/119) | Attachments `file://` borrados: degradan a placeholder en vez de crash de carga de sesión (6 call sites) | v0.16.1 |
| **I-127** | [#127](https://github.com/phoson-lat/phoson-engine-minimal/issues/127) | `timeout` por invocación en bash (default 30s, sin tope por decisión del owner) + extensión a sub-agents | v0.17.0 |
| **I-112** | [#112](https://github.com/phoson-lat/phoson-engine-minimal/issues/112) | `UserWarning` duplicado a stderr: hooks de warnings + dedup; notice estilizado único | v0.17.1 |
| **I-110** | [#110](https://github.com/phoson-lat/phoson-engine-minimal/issues/110) | Plugin system: hooks opcionales (comandos, verbos, temas, UI) + `plugins` en config + `phoson-cli plugin *` | v0.18.0 |
| **I-126** | [#126](https://github.com/phoson-lat/phoson-engine-minimal/issues/126) | `phoson_plugin_monitor`: monitores de larga duración (interval/file/command) + wake persistente que reactiva al agente | v0.19.0 |
| **I-115** | [#115](https://github.com/phoson-lat/phoson-engine-minimal/issues/115) | README refrescado (paridad 35/35 comandos, 15/15 flags), deep dives a `docs/cli/`, assets VHS | PR i-115 |
| **I-91** | [#91](https://github.com/phoson-lat/phoson-engine-minimal/issues/91) | Gate de auto-compact conservador (incluye overhead de tools/reasoning) + rescate ante 400 de contexto | v0.13.5 |
| **I-88** | [#88](https://github.com/phoson-lat/phoson-engine-minimal/issues/88) | Header live tokens/costo + costo USD real en OpenRouter | v0.13.6 |
| **I-89** | [#89](https://github.com/phoson-lat/phoson-engine-minimal/issues/89) | `/model` persiste el provider junto con el modelo en `config.toml` | v0.13.7 |
| **I-82** | [#82](https://github.com/phoson-lat/phoson-engine-minimal/issues/82) | vLLM Qwen3.x 400 — cerrado: error de vLLM, no del engine | — |
| **I-83** | [#83](https://github.com/phoson-lat/phoson-engine-minimal/issues/83) | Paneles de error compactados a 1 línea, sobreescribiendo en reintentos | v0.13.8 |
| **I-84** | [#84](https://github.com/phoson-lat/phoson-engine-minimal/issues/84) | CPU TUI: idle/streaming reducidos (29.6% → 4.1%, benchmark I-84) | v0.13.9 |
| **I-108** | [#108](https://github.com/phoson-lat/phoson-engine-minimal/issues/108) | Alt+Backspace ya no se lee como doble-Esc (cancel/rewind accidental) | v0.15.0 |
| **I-109** | [#109](https://github.com/phoson-lat/phoson-engine-minimal/issues/109) | Rewind picker: orden nuevo→viejo, solo mensajes user | v0.15.0 |
| **I-113** | [#113](https://github.com/phoson-lat/phoson-engine-minimal/issues/113) | OpenRouter sort por `agentic_index` + picker unificado `/model`/`/provider` con `unavailable` | v0.13.10/11 |
| **I-100** | [#100](https://github.com/phoson-lat/phoson-engine-minimal/issues/100) | Toggle granular MCP por servidor y por herramienta | v0.13.12 |
| **I-93** | [#93](https://github.com/phoson-lat/phoson-engine-minimal/issues/93) | Binarios standalone (Linux/macOS/Windows) en CI | v0.15.0 |

---

## Principios de desarrollo

1. **Mantener paridad entre frontends:** Cualquier render nuevo debe ser una función pura en `formatting.py` utilizable en modo fullscreen y clásico.
2. **Cobertura de tests rigurosa:** Cada corrección o feature debe incluir tests unitarios/e2e y pasar validación estricta de `ruff` y `pyright`.
3. **Optimización con métricas:** Todo cambio de performance (CPU, tokens, tiempo) debe incluir benchmark o medición verificable.
4. **Contratos falsables (nuevo, Sprint 1 en adelante):** Todo cambio de harness declara qué tareas del set de H-1 predice que arregla y cuáles pone en riesgo; el gate nightly lo verifica. Sin gate, no se declara mejora.
5. **Cada pieza del harness documenta la asunción que la justifica** (el modelo no puede X) para poder retirarla cuando el modelo base deje de necesitarla.
