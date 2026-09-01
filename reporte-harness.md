# HARNESS REVIEW — phoson-engine-minimal / phoson-cli

> **Origen:** revisión externa del harness contra el estado del arte de *harness
> engineering* (2024–2026). El criterio de toda la revisión es uno solo:
> **modelo congelado, ¿cuánto se mueve la tasa de éxito cambiando solo el
> software alrededor?**
>
> **Alcance de la revisión:** hecha sobre documentación pública del repo
> (`README.md`, `ROADMAP.md`, `TODO.md`, `IMPROVEMENTS.md`), **no sobre el
> código fuente**. Varios ítems pueden estar parcial o totalmente
> implementados sin estar documentados — validar antes de abrir issue.
>
> **Estado de referencia asumido:** v0.18.0 · ~1600 tests passing · pyright 0
> errors · ruff clean.
>
> **Idioma:** español, siguiendo la convención de `ROADMAP.md` /
> `IMPROVEMENTS.md` (plan de trabajo interno, fuera del alcance de la política
> English-only que cubre contenido user-facing).

---

## Tabla resumen

| ID | Título | Prioridad | Esfuerzo | Impacto |
|---|---|---|---|---|
| **H-1** | Set de evaluación de agente + gate de no-regresión en CI | **P0** | L | 🔴 Crítico (hoy no existe función de fitness) |
| **H-2** | Exportación de trazas: `phoson_plugin_otel` | **P0** | M | 🔴 Alto (precondición de H-1 y de atribución de fallas) |
| **H-3** | Middleware de detección de doom loops | **P1** | S-M | 🟠 Medio (quema de presupuesto en ciclos estériles) |
| **H-4** | Inyección de contexto ambiental (presupuesto de turnos/tiempo) | **P1** | S | 🟠 Medio (el agente no prioriza si no sabe qué le queda) |
| **H-5** | Reasoning effort por fase ("reasoning sandwich") | **P1** | M | 🟠 Medio (perilla ya existente, mal aprovechada) |
| **H-6** | Permisos por intención + anotaciones MCP + audit log | **P1** | M-L | 🔴 Alto (granularidad actual no captura el riesgo real) |
| **H-7** | Presupuesto de wall-clock en modo no-interactivo | **P1** | S | 🟠 Medio (one-shot no tiene Esc) |
| **H-8** | Paridad docs↔código en CI, extendida al contexto de agente | **P2** | S | 🟡 Medio (deriva documental = contexto envenenado) |
| **H-9** | Compactación controlada por el agente | **P2** | M | 🟡 Medio (hoy es reactiva a umbral) |
| **H-10** | Contrato de handoff para tareas multi-sesión | **P2** | L | 🟡 Medio (engine stateless por run) |
| **H-11** | Presupuesto de tools / carga diferida cache-aware | **P2** | M | 🟡 Medio (crece sin techo con MCP + plugins) |

---

## Contexto: por qué esta lista y no otra

La evidencia acumulada desde 2024 dice que una fracción grande del desempeño
de un agente vive fuera de los pesos del modelo, y que esa fracción es la
palanca barata: no requiere GPUs, es inspeccionable, es portable entre
modelos, y sobrevive al siguiente upgrade del modelo base.

Referencias de deltas medidos con **modelo fijo**:

| Sistema | Benchmark | Delta |
|---|---|---|
| SWE-agent (diseño de interfaz) | SWE-bench Lite | 11.0 → 18.0 |
| LangChain deep agents | Terminal-Bench 2.0 | 52.8 → 66.5 (Top 30 → Top 5) |
| Azure SRE Agent (filesystem sobre tools) | "Intent Met" | 45 → 75 |
| Vercel d0 (15+ tools → 1 bash) | éxito interno | 80% → 100% |
| AHE (7 componentes, evolución automática) | Terminal-Bench 2 | 69.7 → 77.0 |
| HarnessFix (atribución de capa antes de parchear) | SWE-bench Verified | 45 → 57 |
| statewright (guardas de estado, modelos locales) | subset SWE-bench | 2/10 → 10/10 |

El diagnóstico honesto sobre phoson: contra la definición formal de harness
(loop de agente + interfaz de tools + gestión de contexto + mecanismos de
control), **phoson-cli tiene las cuatro**. No es un wrapper de tools, es un
harness completo. Varias decisiones están bien tomadas contra el estado del
arte:

- Skills indexadas a una línea, cuerpo cargado bajo demanda sin romper el
  prompt cache — es exactamente el patrón de progressive disclosure.
- `AGENTS.md` inyectado al system prompt, que es donde debe vivir lo crítico
  porque sobrevive a cualquier compactación.
- Pooling de sesión MCP con mejora medida (~11x).
- Permisos que fallan cerrado en no-interactivo.
- Contrato `Plugin` único y síncrono, con el razonamiento documentado.

El gap principal es de otra categoría, y probablemente se escapa justo porque
el resto está tan cuidado: **no hay forma de saber si un cambio de harness
mejoró o empeoró el agente.**

---

## Contrapeso honesto (leer antes de priorizar)

Tres cosas que acotan el retorno esperado de toda esta lista:

1. **El régimen importa.** El equipo de Terminal-Bench, midiendo modelos
   frontier en tareas duras, encontró el orden inverso al de SWE-agent:
   cambiar el modelo suele ganarle a cambiar el scaffold (+52% relativo por
   swap de modelo contra +17% por swap de scaffold).
2. **El payoff no es monotónico en capacidad.** Pega más fuerte en el tier
   medio: los modelos más débiles no logran activar ni seguir de forma
   confiable un harness editado, y los más fuertes lo necesitan menos.
3. **Dónde sí paga completo en phoson:** el caso vLLM / Ollama / LM Studio
   local, donde el modelo es fijo y más débil. Ahí el resultado de
   statewright (2/10 → 10/10 restringiendo el espacio de tools por fase) es
   directamente aplicable. Si el uso dominante es contra frontier vía
   OpenRouter, bajar expectativas.

Y una advertencia general del campo que aplica al diseño: **cada componente
del harness existe porque asumes que el modelo no puede hacer algo, y esas
asunciones expiran.** Conviene marcar cada pieza nueva con la asunción que la
justifica, para poder retirarla cuando deje de ser cierta.

---

## H-1 — Set de evaluación de agente + gate de no-regresión en CI

- **Prioridad:** **P0** · **Esfuerzo:** L · **Impacto:** 🔴 Crítico
- **Área:** nuevo `bench/` (o el existente, ver nota), `.github/workflows/`

### Problema

Los ~1600 tests, ruff y pyright miden **corrección de software**. Ninguno mide
**calidad de agente**. No existe un número que responda "¿este harness resuelve
más tareas que el de la semana pasada?".

El principio de desarrollo #3 del repo ("Optimización con métricas: todo cambio
de performance debe incluir benchmark") se aplica hoy a CPU (I-84: 29.6% →
4.1%) y latencia MCP (991ms → 91ms). Nunca a tasa de éxito en tareas.

Consecuencia concreta: I-127 quitó el tope de timeout del bash, I-128 metió un
evento nuevo en `_consume_llm_stream()`, I-91 cambió el gate de compactación.
Cada uno pudo haber costado puntos de tasa de éxito y no habría forma de
saberlo.

Esto importa más de lo que parece por una asimetría medida: la auto-atribución
de cambios de harness es ~5× mejor que el azar prediciendo qué tareas
**arreglará** una edición, pero **apenas mejor que el azar prediciendo cuáles
romperá**. Confiable para arreglos, ciega para regresiones. Sin gate, cada
merge de harness es una apuesta sin contraparte.

También hay evidencia directa de que los cambios de harness pueden ser
negativos, no solo neutros: en SWE-agent, quitar el comando `edit` costó −7.7
puntos, y una herramienta de búsqueda **mal diseñada** (12.0%) puntuó **peor
que no tener búsqueda** (15.7%).

### Nota de verificación previa

El repo map del `README.md` lista `bench/` con `run_bench.py` + tasks, pero ese
directorio **no aparece en el listado raíz del repo**. Verificar: si existía y
se movió, es deriva de docs (ver H-8); si nunca existió, este ítem parte de
cero.

### Solución propuesta

1. **Elegir el substrato.** Terminal-Bench 2.0 es la opción natural: 89 tareas
   en contenedores Docker, 16 categorías (SWE, seguridad, cómputo científico,
   data science, debugging), verificadores deterministas. El codebase oficial
   `terminus-2` que se distribuye con el benchmark sirve como baseline honesto
   sin confounds de interfaz.
2. **No correr las 89.** Seleccionar 15–25 tareas por **discriminación**, no
   por dificultad. Una tarea solo ayuda a seleccionar cuando los candidatos
   discrepan en ella: las que todos pasan y las que todos fallan tienen
   varianza cero y no rankean nada, por difíciles que sean. Lo que se busca son
   las tareas de **pass parcial** (algunos rollouts pasan, otros no) — son el
   diagnóstico más valioso.
3. **Fijar el modelo.** Un modelo por corrida, versión pineada. Si cambia el
   modelo, la comparación no vale.
4. **Split honesto.** Un subset de evolución (contra el que se itera) y un
   subset held-out que nunca se optimiza, reportado aparte. Sin esto, se
   sobreajusta el benchmark y no se nota.
5. **Gate en CI.** Job nightly (no por PR — es caro) que corre el set y falla
   si la tasa baja respecto a la baseline registrada. Los empates se rechazan:
   la evidencia de SkillOpt es que las ganancias grandes vienen de 1–4
   ediciones aceptadas de una búsqueda mayormente rechazada. **El gate, no la
   creatividad de la propuesta, es donde se gana la confiabilidad.**

### Criterio de listo

- [ ] `bench/` con N tareas (15–25) y verificadores deterministas, corribles
      con un comando.
- [ ] Baseline registrada en el repo (tasa, modelo, fecha, commit).
- [ ] Workflow nightly que corre el set y publica el resultado; falla si la
      tasa cae por debajo de la baseline menos la varianza medida.
- [ ] La varianza está medida: ≥3 corridas de la baseline para saber cuánto es
      ruido y cuánto es señal.
- [ ] Split held-out documentado y nunca usado para iterar.

---

## H-2 — Exportación de trazas: `phoson_plugin_otel`

- **Prioridad:** **P0** · **Esfuerzo:** M · **Impacto:** 🔴 Alto
- **Área:** nuevo paquete `phoson_plugin_otel/`

### Problema

El `README.md` lista "Add observability integrations (OpenTelemetry, Langfuse)"
bajo *Ideas for contributions*. Debería subir de idea-para-terceros a trabajo
propio de P0, porque es la **precondición de H-1**: sin trazas exportables no
se pueden comparar corridas ni clasificar modos de falla a escala, solo verlos
de uno en uno en la TUI.

### Por qué es más barato de lo que parece

**La parte difícil ya está hecha.** El repo ya tiene un modelo de eventos más
rico que el de mucha gente que sí exporta:

- `LLMEvent` normalizado con 10 tipos (`LLMStartEvent`, `TokenEvent`,
  `ReasoningStart/Token/Done`, `ToolCallDelta`, `ToolCall`, `Usage`, `LLMDone`,
  `Error`).
- Eventos de agente: `AgentToolComposingEvent`, `AgentToolStart/Done`,
  `AgentStepDoneEvent`, `AgentDone`, `AgentError`.
- `RunStep`, `SessionMetrics`, costo en USD por step.

Lo que falta es únicamente el **sink**.

### Solución propuesta

- Plugin que mapee el stream de eventos a spans jerárquicos:
  `run → step → llm_call` y `run → step → tool_call`.
- Atributos por span: modelo, provider, tokens (input / output / cached /
  reasoning), costo USD, latencia, nombre de tool, resultado (ok/error/timeout/
  denied-by-permission), decisión de permiso aplicada.
- Correlación por `session_id` + `run_id` + `step_index` como IDs compartidos,
  para poder cruzar contra cualquier otra capa del stack del consumidor.
- Exportador OTLP estándar (que cualquiera lo apunte a donde quiera: Langfuse,
  LGTM, Honeycomb). No acoplar el plugin a un vendor.
- Seguir las convenciones de los plugins oficiales (`_plugin.py`,
  `plugin = MyPlugin()`, README, entrada en "Bundled plugins" de
  `docs/plugins.md`).

### Qué desbloquea

- **H-1** (comparar corridas).
- **Atribución de fallas por capa** — el aporte central de HarnessFix: compilar
  trayectorias fallidas y atribuir cada falla a **una** capa del harness antes
  de editarla ("localiza antes de arreglar"). Ese solo movimiento levantó un
  baseline fuerte diseñado a mano entre 15% y 50% relativo en cuatro
  benchmarks.
- **Adopción externa.** Un equipo que meta el engine en producción va a
  preguntar por OTel antes que por temas.

### Criterio de listo

- [ ] Un run genera un trace completo en un colector OTLP local, con la
      jerarquía run/step/tool visible.
- [ ] Tokens y costo por span cuadran con lo que reporta `/cost` y `/tokens`.
- [ ] Los 3 plugins oficiales existentes siguen funcionando sin cambios.
- [ ] Overhead medido: el plugin activo no degrada latencia de forma
      perceptible (benchmark, siguiendo el principio #3 del repo).

---

## H-3 — Middleware de detección de doom loops

- **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio
- **Área:** `phoson_agent/` (middleware), `phoson_cli/`

### Problema

Los controles de terminación hoy son `max_turns` y `subagent_timeout_seconds`.
Ninguno detecta que **el agente corrió el mismo comando fallido cinco veces**.
El run consume su presupuesto completo en un ciclo estéril y termina por
agotamiento, no por decisión.

La detección de loops fue uno de los ingredientes explícitos del salto de
LangChain (52.8 → 66.5 en Terminal-Bench 2.0, sin cambiar modelo).

### Solución propuesta

- Middleware sobre el hook de tool call que mantenga un historial de
  `hash(tool_name, args_normalizados)` dentro del run.
- Al detectar N repeticiones (arrancar con N=3) de la misma tupla **con el
  mismo resultado de error**, actuar. Dos modos, configurables:
  - **Inyectar**: agregar al contexto una observación explícita
    ("ya intentaste esto 3 veces con el mismo error; prueba otra cosa").
    Menos invasivo, preferible como default.
  - **Cortar**: terminar el run con un error estructurado.
- Normalización de args: importante que `bash("pytest -q")` y
  `bash("pytest -q ")` colisionen. Whitespace, orden de keys en dicts.
- Configurable y desactivable (`PHOSON_LOOP_DETECT_N`, `0` = off).

### Criterio de listo

- [ ] Test: un stream simulado que repite la misma tool call fallida N veces
      dispara la acción configurada.
- [ ] Test: repeticiones legítimas (misma tool, args distintos; o misma tupla
      con resultado distinto) **no** disparan.
- [ ] Medido contra el set de H-1: no baja la tasa de éxito.

---

## H-4 — Inyección de contexto ambiental (presupuesto de turnos/tiempo)

- **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio
- **Área:** `phoson_agent/` (middleware)

### Problema

El agente no sabe cuántos turnos le quedan ni cuánto tiempo lleva. Si no lo
sabe, no prioriza: gasta igual el turno 2 que el turno 18 de 20.

Hay evidencia de que la conciencia temporal es **ortogonal a la capacidad de
razonamiento**: dar feedback temporal explícito dentro del loop mejora de forma
significativa el desempeño en tareas con restricción de deadline. No es algo
que se pueda asumir a partir de que el modelo sea bueno.

LangChain reporta ganancia de inyectar dos cosas vía middleware: mapa de
directorio y **avisos de presupuesto de tiempo**.

### Solución propuesta

- Middleware `before_model` que inyecte un bloque corto y estable:
  - Turno actual / máximo (`step 12/20`).
  - Tiempo transcurrido y presupuesto restante si hay uno (ver H-7).
  - Opcional: fracción de ventana de contexto consumida (ya se calcula para el
    gate de auto-compact de I-91 — reutilizarlo).
- **Restricción de diseño:** debe ir al **final** del contexto, no al inicio.
  Cualquier cosa que cambie en el prefijo invalida el KV-cache de todo lo que
  sigue, y el prompt caching del CLI (50–90% de ahorro reportado en sesiones
  largas) depende de que el prefijo sea estable.
- Mapa de directorio: solo si el CWD es un repo y por debajo de un tope de
  tamaño; degradar a nada si excede.

### Criterio de listo

- [ ] El bloque aparece al final del contexto, verificable en un test que
      inspeccione el payload enviado.
- [ ] Test de regresión de caché: la fracción de tokens cacheados no cae
      respecto a la baseline actual.
- [ ] Medido contra el set de H-1.

---

## H-5 — Reasoning effort por fase ("reasoning sandwich")

- **Prioridad:** **P1** · **Esfuerzo:** M · **Impacto:** 🟠 Medio
- **Área:** `phoson_agent/`, `phoson_cli/commands.py`

### Problema

`/reasoning-effort` es hoy un control **global** de sesión (`low`…`max`,
`off`). Pero el razonamiento no vale lo mismo en todas las fases del turno:
planear y verificar se benefician mucho; ejecutar un `cat` mecánico, nada.

El patrón medido por LangChain (concentrar el máximo thinking en planeación y
verificación, mínimo en ejecución) fue uno de los tres pasos de su progresión:
53.9% → 63.6% → 66.5%.

### Solución propuesta

- Perfil de effort por fase, configurable, con default conservador:
  `planning: high` / `execution: low` / `verification: high`.
- La detección de fase puede empezar simple y heurística: primer step del run =
  planning; step tras una tool de test/build fallida = verification; resto =
  execution. No hace falta clasificador.
- Respetar el override global: si el usuario fija `/reasoning-effort max`,
  gana.
- **Restricción de API:** en algunos providers el modo de thinking no puede
  cambiar a mitad de turno, y los bloques de thinking deben preservarse al
  devolver resultados de tool (omitirlos rompe silenciosamente el razonamiento
  multi-paso). Verificar el comportamiento por adapter antes de generalizar.

### Criterio de listo

- [ ] Perfil configurable en `config.toml`, con el default documentado.
- [ ] Los bloques de thinking se preservan correctamente al pasar tool results
      (test por adapter que lo soporte).
- [ ] Medido contra el set de H-1: reportar tasa **y** costo, porque este
      cambio mueve ambos.

---

## H-6 — Permisos por intención + anotaciones MCP + audit log

- **Prioridad:** **P1** · **Esfuerzo:** M-L · **Impacto:** 🔴 Alto
- **Área:** `phoson_cli/` (permisos), `phoson_plugin_mcp/`

### Problema

El modelo actual (`allow`/`ask`/`deny` por tool + allow-patterns, editable en
runtime, fail-closed en no-interactivo) es más de lo que tienen la mayoría de
los CLIs. Pero la **granularidad es la equivocada**.

1. **Nombre de tool no es unidad de riesgo.** Un `allow` de `bash` no significa
   nada: `bash` con `ls` y `bash` con `rm -rf` son decisiones distintas. El
   mismo binario es benigno o destructivo según sus argumentos. Las
   allow-patterns son un parche de regex sobre ese problema, y regex es
   exactamente la clase de defensa que colapsa ante entradas adaptativas.
2. **La trifecta letal ya está expuesta.** Con MCP servers como tools de
   primera clase, el harness combina *acceso a datos privados* + *exposición a
   contenido no confiable* + *comunicación externa*. Analizar tools de forma
   aislada no captura el riesgo que emerge de sus **combinaciones**.
3. **Va a llegar la fatiga de aprobación.** Medición de referencia: los
   usuarios aprueban el **93%** de los prompts de permiso, lo que convierte la
   aprobación en ruido ceremonial. Un nivel `ask` que se aprueba siempre no es
   una defensa.

El encuadre correcto, y el consenso de 2026: **en un agente con tools, la
inyección de prompts es un problema de autorización, no de contenido — el daño
no es una oración mala, es una acción.** La primera generación de defensas
trató el problema como contenido (clasificadores, guardrails, detectores de
jailbreak) y perdió ante atacantes adaptativos.

### Solución propuesta (3 fases, en orden)

**Fase 1 — Taxonomía de intención.** Reemplazar allow/deny por nombre con
categorías derivadas de la tool **más sus args parseados**:
`filesystem_read`, `filesystem_write`, `filesystem_delete`, `network_outbound`,
`process_spawn`, `lang_exec`. La política se escribe contra la taxonomía, no
contra nombres de comando. Mantener compatibilidad hacia atrás con
`permissions.json` actual durante una versión.

**Fase 2 — Anotaciones MCP como vocabulario de riesgo.** Consumir
`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` del
protocolo MCP como entrada a la decisión de permiso. Son *hints*, no contratos
— tratarlos como señal, no como garantía — pero es el vocabulario estándar y ya
viene en el protocolo que el plugin consume. Extiende naturalmente el trabajo
de I-100 (toggle por servidor/tool).

**Fase 3 — Registro de auditoría pre-acción y anti-fatiga.**
- Cada decisión de permiso queda registrada de forma estructurada (no solo
  mostrada en la TUI): quién pidió qué, con qué intención derivada, qué política
  aplicó, qué se decidió. Referencia de viabilidad: hay implementaciones que
  hacen intercepción síncrona con política declarativa y registro firmado en
  **mediana de 53 ms**; en un testbed adversarial, política restrictiva dio 0%
  de éxito de ataque contra 74.6% con política permisiva.
- Contra la fatiga: gate rápido primero, razonamiento caro solo sobre lo
  marcado. Dos decisiones de diseño que vale copiar y que la gente suele
  omitir:
  - **Negar y continuar** en lugar de detener el run. El agente recibe una
    denegación estructurada como observación y sigue.
  - **Quitar los mensajes del assistant del contexto del clasificador**, para
    que el agente no pueda racionalizar ante su propio guardián la acción
    peligrosa que acaba de proponer.

### Criterio de listo

- [ ] Una política escrita contra intenciones bloquea `bash("rm -rf /")` y
      permite `bash("ls")` sin listar comandos por nombre.
- [ ] Las anotaciones MCP se leen y alimentan la decisión; tools sin
      anotaciones degradan al default seguro.
- [ ] Cada decisión produce un registro estructurado, exportable vía H-2.
- [ ] Migración: `permissions.json` existente sigue funcionando.
- [ ] Suite de casos adversariales (contenido con instrucciones inyectadas en
      salida de tool / recurso MCP) donde ninguna acción fuera de política se
      ejecuta.

---

## H-7 — Presupuesto de wall-clock en modo no-interactivo

- **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio
- **Área:** `phoson_cli/tools/_timeouts.py`, `phoson_cli/__main__.py`

### Problema

La decisión de I-127 de no poner tope máximo al `timeout` de `bash` está bien
razonada **para uso interactivo**: builds y entrenamientos largos son legítimos,
y el escape ante un cuelgue es cancelar el run con Esc.

Pero **one-shot mode no tiene Esc**. `phoson-cli "task"` dentro de un pipeline
de CI, con un comando colgado y un timeout que el agente puede fijar en lo que
quiera, corre hasta que algo externo lo mate.

Es un hueco de categoría, no un desacuerdo con la decisión: el mecanismo de
escape que justifica la ausencia de tope no existe en ese modo.

### Solución propuesta

- Presupuesto a nivel de **run** (no de tool), aplicable **solo en modo
  no-interactivo** (one-shot / stdin piped): `PHOSON_RUN_BUDGET_SECONDS`,
  default razonable (p. ej. 600s), `0` = sin límite para quien lo quiera así
  explícitamente.
- Al agotarse: terminar limpio con exit code distinto de 0 y un mensaje
  identificable (no un traceback), cerrando plugins y sesiones MCP como ya hace
  `_rebuild_engine`.
- No tocar el comportamiento interactivo. La decisión del owner se mantiene
  donde aplica.
- Se conecta con H-4: el presupuesto restante es justo lo que conviene
  inyectar al contexto.

### Criterio de listo

- [ ] Test: one-shot con una tool que cuelga termina en el presupuesto con exit
      code 1 y mensaje limpio.
- [ ] Test: el modo interactivo no cambia (sin tope, Esc sigue siendo el
      escape).
- [ ] Documentado en `docs/cli/` y en la tabla de env vars del README.

---

## H-8 — Paridad docs↔código en CI, extendida al contexto de agente

- **Prioridad:** **P2** · **Esfuerzo:** S · **Impacto:** 🟡 Medio
- **Área:** `.github/workflows/ci.yml`, `scripts/`

### Problema

Hay deriva documental detectable hoy:

- `TODO.md` dice que Textual (TUI full-screen) fue "explícitamente rechazado";
  el `README.md` describe la TUI full-screen como el frontend **default**.
- `TODO.md` lista el modelo de permisos por tool como **P3 sin empezar**
  ("hoy `safe_mode` es all-or-nothing"); el `README.md` lo lista como shipped.
- El repo map del `README.md` incluye `bench/`, que no aparece en el listado
  raíz.

`TODO.md` se declara índice histórico, así que parte de esto está explicado —
pero un lector externo no lo sabe, y hay un problema más serio:

**`AGENTS.md` se inyecta al system prompt.** Cualquier documento del repo que
un agente pueda cargar como contexto y esté desfasado no es solo mala
documentación: es **contexto envenenado**. En un repo cuya tesis es que los
agentes construyen software, conviene aplicarse la práctica a sí mismo — la
gestión de entropía (reparar deriva documental de forma periódica) está
identificada como una de las tres piezas de la disciplina de harness
engineering.

Precedente propio: para I-115 ya construyeron un script de extracción que
verificó paridad de comandos 35/35 y flags 15/15. La infraestructura existe.

### Solución propuesta

- Extender ese script a un check de CI que corra en cada PR.
- Cubrir: comandos slash, flags CLI, env vars, y la lista de "Bundled plugins".
- Marcar explícitamente en el header de `TODO.md` que es histórico y que la
  fuente de verdad es `IMPROVEMENTS.md` (ya lo dice; hacerlo más visible).
- Auditar todo archivo que pueda entrar al contexto del agente (`AGENTS.md`,
  `CLAUDE.md`, skills) contra el estado real. Considerar un presupuesto de
  tamaño para `AGENTS.md` con warning al excederlo: los archivos de
  instrucciones inflados cuestan contexto en cada request y hay evidencia de
  que los generados automáticamente pueden costar 20%+ en tokens sin aportar
  proporcionalmente.

### Criterio de listo

- [ ] CI falla si un comando/flag/env var existe en el código y no en los docs,
      o viceversa.
- [ ] Los tres desyncs listados arriba están resueltos o explicados en su
      documento.
- [ ] Existe un tope de tamaño para `AGENTS.md` con aviso (no bloqueante).

---

## H-9 — Compactación controlada por el agente

- **Prioridad:** **P2** · **Esfuerzo:** M · **Impacto:** 🟡 Medio
- **Área:** `phoson_agent/plugins/summarizer.py`

### Problema

I-91 dejó la compactación en buen estado: gate conservador a nivel de request
(incluyendo overhead de schemas de tools y reasoning) más rescate de emergencia
ante 400 del provider. Eso resuelve el bug.

Lo que queda es un tema de diseño: la compactación es **reactiva a umbral**, y
el umbral no sabe nada de la tarea. Puede dispararse a mitad de una subtarea e
interrumpir el estado de razonamiento en vuelo.

El movimiento de 2026 es pasar el control al agente: una tool dedicada que el
agente llama cuando le conviene estratégicamente — entre tareas, o antes de
consumir un input grande. Reportado: 22.7% menos tokens sin pérdida de accuracy
en tareas de horizonte largo, y elimina el modo de falla de compactar a
destiempo.

Advertencia asociada, importante: **dejar que un LLM reescriba
monolíticamente su propio contexto lo erosiona.** Hay un caso documentado de un
solo paso de reescritura libre que llevó el contexto de 18,282 a 122 tokens con
la accuracy cayendo de 66.7 a 57.1. Los sistemas durables usan ediciones
**incrementales con gate**, nunca reescrituras libres.

### Solución propuesta

- Exponer `compact_context()` como tool (con permiso propio, default `allow`),
  además del umbral automático que se queda como red de seguridad.
- Documentar en el system prompt cuándo conviene llamarla (entre tareas, antes
  de leer algo grande).
- Mantener el handoff **estructurado** que ya usan; no permitir reescritura
  libre del historial.
- Documentar qué sobrevive a una compactación y qué no. Regla derivada de la
  práctica: **nunca depender de la compactación para reglas críticas** — esas
  van a `AGENTS.md`/system prompt, que sobrevive a todo.
- Referencia de diseño para más adelante: compactación progresiva por etapas
  (reducción de presupuesto → snip → microcompact → colapso → auto-compact) en
  lugar de un solo escalón.

### Criterio de listo

- [ ] La tool existe, es llamable, y su efecto es idéntico al de `/compact`.
- [ ] El umbral automático sigue funcionando como fallback.
- [ ] `docs/cli/compaction.md` documenta qué sobrevive y qué se pierde.
- [ ] Medido contra el set de H-1 en las tareas más largas del set.

---

## H-10 — Contrato de handoff para tareas multi-sesión

- **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟡 Medio
- **Área:** `phoson_agent/`, `phoson_plugin_checkpoint/`

### Problema

El `ROADMAP.md` lo dice explícitamente (en el contexto de I-126): el engine es
stateless por run — cuando `AgentEngine.run()` regresa, nada persiste ni se
reprograma.

I-126 (monitores de larga duración) ataca el **disparo externo**, no la
**continuación de tarea**. Son problemas distintos: "despiértame cuando pase X"
no es lo mismo que "sigue donde te quedaste con la tarea que excede una ventana
de contexto".

El patrón de referencia para lo segundo: un agente **inicializador** que prepara
el entorno una vez y entrega a un agente de **trabajo** que hace progreso
incremental por sesión, con **artefactos estructurados de handoff** — lista de
features, commits, gates de test — como el estado que cruza ventanas de
contexto. Variantes de producción llevan esto a checkpointing
hibernate-and-wake para reanudar tareas de horas sin perder contexto.

Ya tienen la mitad de la infraestructura: `ConversationTree` +
`phoson_plugin_checkpoint` (Postgres, async real). Falta el **contrato**.

### Solución propuesta

- Definir un artefacto de handoff explícito y versionado (no un resumen en
  prosa): estado de la tarea, decisiones tomadas, qué falta, qué se intentó y
  falló, punto de reanudación.
- Persistirlo junto al `ConversationTree` en el checkpoint, no dentro del
  historial de mensajes (para que sobreviva a compactación).
- Un modo de arranque que reanude desde el handoff en vez de desde el
  historial completo.
- Decidir si vive en el engine o es un plugin. Sugerencia: contrato y
  serialización en `phoson_agent`, persistencia en `phoson_plugin_checkpoint`
  — mismo criterio que se usó para separar tipos neutrales de render en I-110.

### Criterio de listo

- [ ] Una tarea que excede la ventana de contexto se completa a lo largo de ≥2
      runs con progreso verificable, sin reiniciar desde cero.
- [ ] El handoff sobrevive a un reinicio del proceso.
- [ ] Test de integración contra Postgres real (mismo patrón
      skip-si-no-hay-servicio que ya usan).

---

## H-11 — Presupuesto de tools / carga diferida cache-aware

- **Prioridad:** **P2** · **Esfuerzo:** M · **Impacto:** 🟡 Medio
- **Área:** `phoson_agent/`, `phoson_plugin_mcp/`

### Problema

Con 20+ providers, MCP servers, plugins, sub-agentes (`agent`/`agents`) y
skills, las definiciones de tools crecen sin techo y consumen contexto en
**cada** request.

Dato de referencia del problema y de la solución: sistemas internos que
llegaron a **134K tokens solo en definiciones de tools**; una capa de
descubrimiento bajo demanda los bajó de **77K a 8.7K** (85%), con precisión en
evals de MCP subiendo de 79.5% a 88.1% en el modelo más fuerte medido.

I-100 dio toggle manual por servidor y tool. Es la versión artesanal de esto y
ya ayuda; lo que falta es que no dependa de que el usuario recuerde apagar
cosas.

### Restricción de diseño crítica (leer antes de implementar)

**No remover tools dinámicamente a mitad de sesión.** Dos razones, ambas
medidas en producción:

1. Las definiciones de tools viven al **inicio** del contexto tras
   serializar, así que cualquier cambio **invalida el KV-cache de todo lo que
   sigue**.
2. Cuando acciones y observaciones previas referencian tools que ya no están
   definidas, el modelo se confunde y produce violaciones de schema o acciones
   alucinadas.

La solución de referencia es **enmascarar en vez de remover**: máquina de
estados sensible al contexto que enmascara logits durante el decoding, con
prefijos consistentes por grupo (`browser_*`, `shell_*`) para enmascarado por
grupos.

Esto importa especialmente aquí porque el CLI ya reporta 50–90% de ahorro por
prompt caching en sesiones largas. **Una carga diferida mal diseñada canjea un
ahorro chico por la pérdida de uno grande.** Medir ambos antes y después.

### Solución propuesta

1. **Primero medir.** Instrumentar cuántos tokens ocupan las definiciones en
   una sesión típica con MCP + plugins cargados. Puede que no sea un problema
   todavía; el dato decide la prioridad.
2. Si lo es: descubrimiento bajo demanda con **prefijo estable**. Las
   definiciones completas se cargan en una posición del contexto que no rompa
   el prefijo cacheable, o se enmascaran sin remover.
3. Nota realista sobre el ranking: la recuperación de tools no está resuelta —
   una comparativa independiente midió 56% de precisión con regex y 64% con
   BM25 sobre 4,027 tools. La feature ahorra contexto; no garantiza que
   encuentre la tool correcta. Si se implementa, medirlo.

### Criterio de listo

- [ ] Reporte de tokens de definiciones en 3 configuraciones típicas
      (sin MCP / con 2 MCP / con MCP + plugins + skills).
- [ ] Si se implementa: fracción de tokens cacheados **no cae** respecto a la
      baseline (test de regresión).
- [ ] Precisión de recuperación medida sobre el catálogo real.

---

## Roadmap sugerido de ataque

```
Sprint 1 — Medir antes de optimizar  (habilita todo lo demás)
├── H-1  Set de evaluación + gate de no-regresión        P0 · L
└── H-2  phoson_plugin_otel                              P0 · M

Sprint 2 — Ganancias baratas sobre middleware existente
├── H-3  Detección de doom loops                         P1 · S-M
├── H-4  Contexto ambiental (presupuesto)                P1 · S
└── H-7  Wall-clock budget en no-interactivo             P1 · S

Sprint 3 — Control y seguridad
├── H-6  Permisos por intención (3 fases)                P1 · M-L
└── H-5  Reasoning effort por fase                       P1 · M

Sprint 4 — Higiene y horizonte largo
├── H-8  Paridad docs↔código en CI                       P2 · S
├── H-9  Compactación controlada por el agente           P2 · M
├── H-11 Presupuesto de tools (medir primero)            P2 · M
└── H-10 Contrato de handoff multi-sesión                P2 · L
```

**El orden no es negociable en un punto:** H-1 y H-2 van primero. Sin ellos,
los otros nueve ítems son opinión y no hay forma de comprobar si mejoraron algo.
Con ellos, cada uno de los otros nueve se vuelve una hipótesis falsable.

Una práctica que vale adoptar junto con el gate: que **cada PR de harness
declare un contrato falsable** — qué tareas del set predice que arregla y
cuáles pone en riesgo — y que se verifique en la corrida siguiente. Convierte
prueba-y-error en prueba de hipótesis, y es el mecanismo que usan los sistemas
que mejor documentan sus ganancias.

---

## Referencias

**Fundacionales**
- SWE-agent — *Agent-Computer Interfaces Enable Automated Software Engineering* · https://arxiv.org/abs/2405.15793
- Anthropic — *Effective Context Engineering for AI Agents* · https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic — *Writing Effective Tools for Agents* · https://www.anthropic.com/engineering/writing-effective-tools-for-agents
- Manus — *Context Engineering for AI Agents: Lessons from Building Manus* · https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Chroma — *Context Rot: How Increasing Input Tokens Impacts LLM Performance* · https://www.trychroma.com/research/context-rot

**Harness engineering**
- OpenAI — *Harness Engineering* · https://openai.com/index/harness-engineering/
- LangChain — *Improving Deep Agents with Harness Engineering* · https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering
- LangChain — *The Anatomy of an Agent Harness* · https://blog.langchain.com/the-anatomy-of-an-agent-harness/
- Martin Fowler / Böckeler — *Harness Engineering* · https://martinfowler.com/articles/harness-engineering.html
- *What makes a harness a harness* (definición formal) · https://arxiv.org/abs/2606.10106
- Microsoft — *Context Engineering Lessons from Building Azure SRE Agent* · https://techcommunity.microsoft.com/blog/appsonazureblog/context-engineering-lessons-from-building-azure-sre-agent/4481200/
- Vercel — *We removed 80% of our agent's tools* · https://vercel.com/blog/we-removed-80-percent-of-our-agents-tools

**Evolución automática y atribución de fallas**
- AHE — *Agentic Harness Engineering* · https://arxiv.org/abs/2604.25850
- HarnessFix — *From Failed Trajectories to Reliable LLM Agents* · https://arxiv.org/abs/2606.06324
- SkillOpt · https://arxiv.org/abs/2605.23904
- Terminal-Bench · https://github.com/laude-institute/terminal-bench
- Blog de síntesis (Jiaxin Zhang) — *Self-Evolving Agentic Harnesses* · https://jxzhangjhu.github.io/blog/2026/self-evolving-agentic-harnesses/

**Seguridad y permisos**
- Anthropic — *Beyond Permission Prompts* · https://www.anthropic.com/engineering/beyond-permission-prompts
- Anthropic — *Claude Code Auto Mode: A Safer Way to Skip Permissions* · https://www.anthropic.com/engineering/claude-code-auto-mode
- MCP — *Tool Annotations as Risk Vocabulary* · https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/
- *Adaptive Evaluation of Out-of-Band Defenses Against Prompt Injection* · https://arxiv.org/abs/2606.26479
- Open Agent Passport (OAP) · https://arxiv.org/abs/2603.20953
- OWASP LLM06:2025 — *Excessive Agency* · https://genai.owasp.org/llmrisk/llm062025-excessive-agency/

**Contexto y tools**
- Anthropic — *Code Execution with MCP* · https://www.anthropic.com/engineering/code-execution-with-mcp
- Anthropic — *Advanced Tool Use* · https://www.anthropic.com/engineering/advanced-tool-use
- *Active Context Compression* · https://arxiv.org/abs/2601.07190
- Anthropic — *Effective Harnesses for Long-Running Agents* · https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents

**Listas curadas**
- https://github.com/ai-boost/awesome-harness-engineering
- https://github.com/RUCAIBox/awesome-agent-harness

---

## Nota final

Esta revisión es externa y se hizo sin acceso al código. Su valor está en el
mapa de prioridades, no en el detalle de implementación — el equipo conoce el
código y va a encontrar que algunos ítems ya están medio hechos y que otros
tienen restricciones que desde afuera no se ven.

Lo único que sostendría sin matices: **el gate de no-regresión (H-1) es lo que
convierte todo lo demás de opinión en ingeniería.**