# BITACORA

Registro previo a cada ejecución. Regla: **si una entrada no está aquí, el comando no se corre.**
Si el host se cae a mitad de un paso, esta bitácora es lo que permite reconstruir qué se estaba
haciendo, con qué comando exacto y dónde quedó la salida.

Formato de cada entrada: acción → autorización → comando exacto → dónde queda la salida → qué
esperaba y cómo se siembra la hipótesis.

---

## Entrada #1 — 2026-10-11 (sesión tras el BSOD de la corrida Docker)

**Contexto.** El host entra en bugcheck; códigos recuperados de Kernel-Power event 41 (el dump no
se escribe: `volmgr 161`, `BugCheckProgress 0x00040049`): hoy dominan `0xEF` CRITICAL_PROCESS_DIED
(7×), `0x154` UNEXPECTED_STORE_EXCEPTION (3×), `0x1E` (1×). `0xEF` es la firma que la propia tabla
de interpretación del journal asigna a **H1** (kill por pid reciclado). De ahí la decisión de correr
la suite **sólo** dentro de un contenedor Linux: namespace de PID propio, kernel Linux — ningún
kill del código bajo prueba puede alcanzar un proceso Windows, crítico o no.

**Autorización del usuario (esta ronda):**
- «B — Docker, suite completa».
- «Pararlo» (monitor `pr279-ci`).

**Acción 1 (sin ejecución de shell).** `stop_monitor('pr279-ci')` → resultado: *el monitor no
existía*, se perdió con el reinicio del host. Nada que parar.

**Acción 2 (LO QUE SE VA A EJECUTAR AHORA).** Suite de tests completa en contenedor Linux.

    cmd //c _scratch_test_docker.cmd -q --timeout=180 ">" _scratch_docker_suite.log 2>&1

efectivamente, dentro del contenedor, el comando que corre es el de `_scratch_test_docker.cmd`:

    docker run --rm -v "%CD%:/src" -w /src \
      -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy \
      ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
      bash -lc "apt-get update ... && apt-get install -y git ripgrep; \
                git config --global --add safe.directory /src; \
                uv sync --dev --locked -q && \
                timeout 1800 uv run --with pytest-timeout pytest \
                  -q --timeout=180 --timeout-method=thread \
                  -q --timeout=180 > _scratch_docker_suite.log 2>&1"

**Salida.** `_scratch_docker_suite.log` en la raíz del repo (dentro del bind-mount `/src`, es decir
en el disco del host): sobrevive a un BSOD, a diferencia del stdout de la herramienta.

**Por qué** `--timeout=180 --timeout-method=thread`: el job `tests` del CI se **colgó** (no falló) en
18% el 22:50:21Z y quedó mudo hasta que lo cancelaste a las 23:19:38Z; `ci.yml` línea 75
(`uv run pytest -q || [ $? -eq 5 ]`) no tiene `timeout-minutes` ni pytest-timeout, así que un cuelgue
es indefinido. Con `--timeout=180` el test atascado se nombra solo, con traceback.

**Hipótesis que esta corrida decide.**
- HANG-1: el cuelgue del CI es `tests/phoson_cli/test_hard_kill_durability.py::`
  `test_cancel_mid_turn_does_not_duplicate_checkpointed_steps`. En Linux, si
  `controller.cancel_current()` devuelve `True` pero la cancelación no alcanza la corrutina del
  stream, el `assert` pasa, `await task` (línea 424) no está acotado y el
  `await asyncio.sleep(3600)` de la línea 413 mantiene el job vivo 60 min sin imprimir nada.
  → Si sale: HANG-1 confirmada, el arreglo es acotar ambas esperas.
- HANG-0: cuelga algún otro test → `--timeout=180` lo nombra igual y se arregla ese.
- Si la suite **pasa completa** en Linux: el cuelgue es específico del runner, no de la rama.

**Riesgo aceptado.** El contenedor corre sobre WSL2/Hyper-V: si el bugcheck fuera de driver o de
hipervisor, Docker *añade* carga. Mitigación: la salida va a fichero, el contenedor lleva
`timeout 1800` interno, y la herramienta tiene tope propio.

**Este comando NO es para el host.** No se corre pytest en Windows en esta sesión.

---

## Entrada #2 — intento fallido + sondas de shell

**Resultado de la entrada #1:** el comando abortó antes de tocar Docker. Salida:

    El sistema no puede encontrar la ruta especificada.
    El sistema no puede encontrar la ruta especificada.

Ambas líneas son el mensaje de `cmd.exe` para un path/comando inválido, y salen dos veces porque
la sustitución `$(pwd -W 2>/dev/null || pwd)` intentó los dos caminos. **Conclusión: la herramienta
`bash` de este host no ejecuta bash, ejecuta `cmd.exe`** (por eso `echo review-only` sí funcionó:
`echo` es un builtin de cmd). Consecuencias: nada de `$(...)`, `||`, `$(pwd)`; el directorio actual
se referencia como `%CD%`, y el `>` dentro de comillas dobles cmd lo respeta.

**Acción 3 (sonda, lo que se ejecuta ahora):** `cd` — sin argumentos, cmd.exe imprime el directorio
actual. Sólo sirve para confirmar la identidad del shell y el cwd.

**Acción 4 (reintento planificado, aún no ejecutado):** mismo contenedor, con `%CD%` en el
bind-mount y el cuerpo complejo movido a un `.sh` dentro del repo (se ejecuta en el contenedor, no
en el host); la cadena que cmd ve queda sin ningún metacaracter:

    docker run --rm -v "%CD%:/src" -w /src -e UV_PROJECT_ENVIRONMENT=/opt/venv
      -e UV_LINK_MODE=copy ghcr.io/astral-sh/uv:python3.12-bookworm-slim
      bash -lc "sed -i 's/\r$//' /src/_scratch_docker_suite.sh; bash /src/_scratch_docker_suite.sh"

El `sed` quita CRLF: un `.sh` con finales de línea Windows rompe dentro de Linux, y el `\r` no se ve
a simple vista en el log (falla con `command not found` sobre nombres que parecen correctos).

---

## Entrada #3 — 2026-10-11 (reanudación tras el BSOD de la corrida Docker)

**Estado encontrado en disco (sólo lectura).** `_scratch_docker_suite.log` contiene **una sola línea**:

    Installed 6 packages in 22ms

Eso es uv instalando `pytest-timeout` dentro del contenedor. Conclusión: el reintento planificado en la
entrada #2 **sí llegó a ejecutarse**, y el host se cayó en ese punto (la corrida Docker y el bugcheck
coinciden). `_scratch_docker_sync.log` está vacío (`uv sync` terminó sin incidencias).

**Consecuencia para las hipótesis.** Ese crash ocurrió con los tests corriendo sobre un kernel Linux
(namespace de PID propio). Debilita **H1** (kill por pid reciclado contra un proceso Windows desde el
código bajo prueba) y apunta a la capa de virtualización/memoria del host — WSL2/Hyper-V, `0x154`
UNEXPECTED_STORE_EXCEPTION. **Caveat:** falta correlacionar la hora de escritura de ese log con la hora
del bugcheck; eso requiere un diagnóstico de sólo lectura (`Get-Item _scratch_docker_suite.log |
Select LastWriteTime`), pendiente de autorización.

**Autorización vigente.** «B — Docker, suite completa» + «Continua». Regla nueva del usuario: **toda
ejecución se escribe aquí antes de correr.**

**Acción 1 — stage 1 (smoke test, se ejecuta ahora).** Mínima carga: valida que Docker responde y que el
contenedor Linux arranca. Pull + arranque son la parte pesada de la corrida anterior, así que se prueban
solos primero.

    docker run --rm ghcr.io/astral-sh/uv:python3.12-bookworm-slim uname -s

Esperado: `Linux`. Si el host sobrevive esto, sigue el stage 2. Si aquí se cae, ya sabemos que el
estresor no son los tests sino el arranque del contenedor (WSL2/Hyper-V) — y eso cambia la
interpretación de todo lo anterior.

**Acción 2 — stage 2 (suite completa, sólo si stage 1 sobrevive).** El cuerpo complejo vive en
`_scratch_docker_suite.sh` (lo parsea bash, no cmd.exe):

    docker run --rm -v "%CD%:/src" -w /src -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy ghcr.io/astral-sh/uv:python3.12-bookworm-slim bash -lc "sed -i 's/\r$//' /src/_scratch_docker_suite.sh; bash /src/_scratch_docker_suite.sh"

Dentro del contenedor eso corre: `apt-get install git ripgrep` → `git config safe.directory` →
`uv sync --dev --locked` → `timeout 2400 uv run --with pytest-timeout pytest -q --timeout=180
--timeout-method=thread`.

**Salida.** `_scratch_docker_suite.log` (suite completa) y `_scratch_docker_sync.log` — ambos dentro del
bind-mount `/src`, o sea **en el disco del host**: sobreviven a un BSOD, a diferencia del stdout de la
herramienta. Tope interno `timeout 2400` en el contenedor; la herramienta lleva tope propio.

**Por qué `--timeout=180 --timeout-method=thread`.** El job `tests` del CI **se colgó** (no falló) en
18% a las 22:50:21Z y quedó mudo hasta que lo cancelaste a las 23:19:38Z. `ci.yml` línea 75
(`uv run pytest -q || [ $? -eq 5 ]`) no tiene `timeout-minutes` ni pytest-timeout, así que un cuelgue es
indefinido. Con `--timeout=180` el test atascado se nombra solo, con traceback.

**Hipótesis que esta corrida decide.**
- **HANG-1**: el cuelgue del CI es `tests/phoson_cli/test_hard_kill_durability.py::`
  `test_cancel_mid_turn_does_not_duplicate_checkpointed_steps`. Línea 413 `await asyncio.sleep(3600)`,
  línea 424 `await task` **sin cota**: si `cancel_current()` devuelve `True` pero la cancelación no
  alcanza la corrutina del stream, el `assert` pasa y el test queda vivo 60 min sin imprimir nada.
- **HANG-0**: cuelga otro test → `--timeout=180` lo nombra igual.
- Si la suite **pasa completa** en Linux: el cuelgue es del runner, no de la rama.

**Riesgo aceptado.** El contenedor corre sobre WSL2/Hyper-V: si el bugcheck fuera de driver/hipervisor,
Docker *añade* carga en vez de quitarla (y la entrada de hoy sugiere exactamente eso). Mitigación: stage 1
aislado antes del stage 2, salida a fichero en disco, `timeout 2400` interno, y **nada de pytest en el
host** (§0 regla 2 del journal).

---

## Entrada #4 — 2026-10-11 · **BSOD durante el stage 2** (segundo consecutivo en Docker)

**Resultado de la entrada #3.**

| stage | comando | resultado |
|---|---|---|
| 1 | `docker run --rm ghcr.io/astral-sh/uv:python3.12-bookworm-slim uname -s` | ✅ **`Linux`** — Docker responde, contenedor arranca, host sobrevive |
| 2 | `docker run ... bash -lc "sed -i 's/\r$//' ...; bash /src/_scratch_docker_suite.sh"` | ❌ **BSOD con el host** — la herramienta reportó *"Run interrupted before this tool completed"* |

**Lo que esto ya descarta y lo que no.**

* **Descartado:** el arranque del contenedor / el pull de la imagen / Docker Desktop por sí solos
  (stage 1 pasó limpio). El estresor está **dentro** del stage 2.
* **Descartado (casi):** H1 como causa de *estos* dos crashes. Stage 2 corrió pytest sobre kernel Linux,
  en su propio namespace de PID: ningún `kill` del código bajo prueba puede alcanzar un proceso Windows.
  Es la segunda vez que el crash cae con los tests en Linux (la primera: entrada #3, `_scratch_docker_suite.log`
  con la única línea de uv).
* **Lo que queda en pie:** el perfil de **carga** de la suite (memoria/IO/CPU) atravesando
  WSL2/Hyper-V hacia el host. Encaja con el conjunto de códigos mezclado de hoy: `0x154`
  UNEXPECTED_STORE_EXCEPTION (pila de almacenamiento/memoria) ×3, `0xEF` CRITICAL_PROCESS_DIED ×7,
  `0x1E` ×1 — un host bajo presión de memoria tumba primero un proceso crítico y luego falla la ruta
  de E/S. Y explicaría la correlación «pytest ↔ BSOD» mejor que cualquier defecto del código investigado:
  la suite de ~4000 tests es, con diferencia, la carga más pesada que corre esta máquina.

**Acción 1 (sólo lectura — diagnosticos read-only, §0 regla 4).** Recuperar lo que quedó en disco antes
del bugcheck:

    type _scratch_docker_suite.log      (sobrevive al BSOD: está en el bind-mount /src)
    type _scratch_docker_sync.log
    git status --short                  (¿quedó algo a medias del contenedor?)

**Acción 2 (lectura, pendiente de autorización).** `Get-Item _scratch_docker_suite.log,
_scratch_docker_suite.log | Select LastWriteTime` + `Get-WinEvent -FilterHashtable
@{LogName='System';ID=41} -MaxEvents 1` para poner **hora del log ↔ hora del bugcheck** y leer el
`BugcheckCode` de este crash. Sin esa correlación, «murió durante la suite» sigue siendo inferencia, no
medición.

**Experimento discriminante propuesto (no ejecutado, requiere autorización).** Cargar la máquina
**sin nada de phoson**: un contenedor que sólo quema memoria/IO (p. ej. `dd`+`head -c` a un tmpfs y un
bucle de malloc) durante unos minutos.
* Si **crashea igual** → H2 (inestabilidad de la máquina) confirmada, el código de phoson queda exonerado
  y la línea de trabajo del engine (durabilidad, CI) puede seguir sin contenedores.
* Si **aguanta** la carga sintética pero muere con la suite → algo *de la suite* importa, y ya no puede
  ser un kill a un proceso Windows: habría que mirar uso de memoria por test y `conftest.py`.

**Riesgo.** Se acepta: el experimento cuesta como máximo otro reinicio, y es el único que separa
«carga» de «código». **Nada de pytest en el host** en ningún caso (§0 regla 2).

---

## Entrada #5 — forense del crash de hoy (**read-only, autorizado: "A — Forense read-only"**)

**Lo que se ejecuta ahora**, todo diagnóstico de sólo lectura (§0 regla 4: *"read-only diagnostics are
presumed safe (they have not been seen to crash)"*), sin un solo test, sin Docker:

1. `powershell -NoProfile -Command "$e=Get-WinEvent -FilterHashtable @{LogName='System';ID=41}
   -MaxEvents 3; ..."` → `BugcheckCode` de los dos crashes de la corrida (vía EFI:
   `BugcheckInfoFromEFI=true`, el único camino cuando no hay dump, §2/§3).
2. `powershell -NoProfile -Command "$e=Get-WinEvent -FilterHashtable @{LogName='System';ID=1001}
   -MaxEvents 3; ..."` → código + parámetros **y el texto completo del mensaje**: en 27/09 WER nombró
   *"Controlador posiblemente relacionado: **dxgkrnl.sys**"*, y eso puede ser todo lo que hace falta.
   (Si no hay `1001`, no es un fallo del query: `volmgr 161` + `BugCheckProgress 0x00040049` significan
   que el dump no se escribió — §2.)
3. `Get-Item _scratch_docker_suite.log | Select LastWriteTime` → **ata la hora del log a la del bugcheck**.
   Es la medición que convierte «murió durante la suite» en dato, en vez de inferencia.
4. `dir C:\Windows\Minidump` → ¿hay `.dmp` nuevo? Ahora que `CrashDumpEnabled=3` quedó validado
   end-to-end con `0xE2` (§10.7), el pipeline puede haber volcado. Requiere elevación (`Acceso denegado`
   en frío, §1 — un listado vacío NO prueba que no se escribió dump).

Los scripts `_scratch_bugcheck41.ps1` y `_scratch_dumpcheck.ps1` ya existen de la sesión anterior: se
reutilizan en vez de reescribirlos (leídos antes de invocar).

**Qué decide este forense.**
- Código `0x154` otra vez → presión de memoria/almacenamiento; `0xEF` → proceso crítico muerto. Ninguno
  de los dos es la firma histórica `0x3B` (§2.2) que el journal venía persiguiendo.
- Si WER nombra un driver en el `1001`: se cierra la investigación con nombre y apellido, y **no** se
  toca el código de phoson.
- `LastWriteTime` pegado al bugcheck ⇒ la suite estaba corriendo de verdad cuando cayó.

**Riesgo.** Mínimo: son consultas a los logs de eventos, sin escritura y sin carga. Si el host se cae
igual, es la prueba definitiva de que no hace falta ninguna acción nuestra para dispararlo.

**RESULTADOS (mismos comandos, ejecutados).**

Códigos de hoy (event 41, hora local):

    00:38:37 0x154 | 00:49:18 0x1E p1=0xffffffffc0000006 | 01:22:54 0xEF | 01:29:53 0xEF
    01:45:51 0xEF  | 02:10:49 0xEF | 02:55:01 0xEF | 03:20:08 0xEF | 03:55:53 0xE2 (provocado)
    14:06:47 0x154 (checkpoint=16) | 17:21:50 0x154 | 17:29:43 0xEF | 17:54:29 0xEF
    18:07:23 0xEF | 18:22:45 0xEF | 18:28:20 0xEF | 18:40:41 0xEF  ← éste mató el stage 2

**Correlación medida:** `_scratch_docker_suite.log` LastWriteTime = **18:37:47**, bugcheck = **18:40:41**
⇒ **2 min 54 s** después de arrancar pytest, con cero salida de pytest en el fichero (arranque/colección,
no un test concreto). `_scratch_docker_sync.log` = 18:37:37.

**Ningún crash de hoy es `0x3B`** — el baseline histórico (§2.2: 26/05 y 27/09, con `dxgkrnl.sys` nombrado
por WER en 27/09) no se repite.

**Cadena de evidencia hacia H2 (máquina), no H1 (nuestro código):**
1. `0x1E` con `p1 = 0xffffffffc0000006` = **STATUS_IN_PAGE_ERROR**: el kernel no pudo leer una página de
   disco. Ruta de memoria/almacenamiento, no un proceso de usuario.
2. `0x154` UNEXPECTED_STORE_EXCEPTION ×3.
3. **Sin `1001` desde las 03:56** (y ese `1001` es el `0xE2` provocado, que sí volcó): el escritor de
   volcados vuelve a fallar en los crashes espontáneos — y usa la misma pila cuya integridad se está
   poniendo en duda. Que falle es síntoma, no trámite.
4. Ventana de la tarde: ~1 crash cada 5-15 min (18:07, 18:22, 18:28, 18:40), muy por encima del
   «~1 cada 23 min» de §0.0.

**Veredicto provisional.** Dos corridas de pytest sobre kernel Linux con namespace de PID propio
terminaron en `0xEF`: ningún kill del código bajo prueba puede salir del contenedor. `0xEF` encaja como
**efecto colateral** de la pila de memoria/almacenamiento rota (muere un proceso crítico primero).
**H1 queda prácticamente descartada para estos crashes; H2 pasa a favorita.**

**Nota de estado:** `admin: False` (la sesión volvió sin elevación tras el reinicio). `CrashDumpEnabled=3`
y `MinidumpDir` sobreviven; el listado de `C:\Windows\Minidump` sigue necesitando elevación.
RAM 23.84 GB, pagefile 9728 MB alloc / uso 0 MB, C: libre 431 GB.

---

## Entrada #6 — parches de CI + test, y el CI a GitHub (**autorizado: "B"**)

**Decisión.** La suite **no se vuelve a correr en este host**, ni en Docker: dos corridas en Linux
terminaron en bugcheck (entradas #3 y #4) y H2 es la favorita. La verificación de estos parches la hace
**Azure** (`ubuntu-latest`), que no toca esta máquina.

**Paso 1 (edición, sin ejecución).** `tests/phoson_cli/test_hard_kill_durability.py`, test
`test_cancel_mid_turn_does_not_duplicate_checkpointed_steps`:
- `await asyncio.sleep(3600)` → `await asyncio.sleep(30)`.
- `outcome = await task` → `outcome = await asyncio.wait_for(task, timeout=30)`.
Razón: con el `await` desnudo, un fallo de la ruta de cancelación cuesta **una hora** de job mudo en vez
de 30 s — exactamente el modo de fallo que dejó al job `tests` en 18% durante 29 minutos.

**Paso 2 (edición, sin ejecución).** `.github/workflows/ci.yml`, job `tests`:
- `timeout-minutes: 20` → el tope que *de verdad* termina el job.
- `uv run pytest -q` → `uv run pytest -q -o faulthandler_timeout=600` → sin plugins nuevos: pytest ya
  trae `faulthandler_timeout`, que **nombra el test atascado** en el log a los 10 min, antes del corte.

**Paso 3 (comandos exactos, en este orden).**

    uv run ruff format tests/phoson_cli/test_hard_kill_durability.py
    uv run ruff check tests/phoson_cli/test_hard_kill_durability.py
    git add .github/workflows/ci.yml tests/phoson_cli/test_hard_kill_durability.py
    git commit -m "ci: cap the tests job and bound the mid-turn cancel test"
    git push

`git add` con **rutas explícitas**: `BITACORA.md`, `BSOD_INVESTIGATION.md` y los `_scratch_*` son
scaffolding y **no** entran al repo (§11 «Deliberately NOT committed»). Nunca `git add -A`.

**Hipótesis que esto verifica.**
- Si el CI vuelve **verde** en la rama ⇒ el cuelgue era ese test (o no reproducible), y el PR #279 queda
  desbloqueado sin gastar un reinicio de esta máquina.
- Si vuelve a **colgarse** ⇒ `faulthandler_timeout` lo nombra en el log y `timeout-minutes` corta a los
  20 min: la próxima vez el coste es 20 minutos de runner, no media hora de tu tiempo.
- Si sale **rojo** por los parches ⇒ son dos cambios de 3 líneas, se revierten sin más.

**Riesgo.** Ninguno en el host: los tres primeros comandos son ruff (binario Rust, barato) y git. El
cuarto lo ejecuta Azure. **Sin pytest local, sin Docker.**

**Revertir.** `git revert <sha>` o deshacer los dos hunks a mano; ninguno toca código de producto.

**RESULTADOS (pasos 1-3 ejecutados).**

    uv run ruff format tests/phoson_cli/test_hard_kill_durability.py   → 1 file left unchanged
    uv run ruff check  tests/phoson_cli/test_hard_kill_durability.py   → All checks passed!
    git status --short                                                 → sólo los 2 ficheros previstos (M)
    git diff --stat                                                    → 2 files, +22 -4
    git commit  → 448b86f  ci: cap the tests job and bound the mid-turn cancel test
    git push    → 60aa819..448b86f  fix/mid-turn-durability
    gh run list → run 38189409096, headSha 448b86f, event pull_request, status queued
                  (el anterior, 60aa819 / 38181956118, quedó cancelled)

Los `_scratch_*`, `BITACORA.md` y `BSOD_INVESTIGATION.md` siguen **sin** entrar al repo: `git add` fue
con rutas explícitas.

---

## Entrada #7 — revisión read-only de la rama, con el CI corriendo en Azure

**Autorización.** Ninguna nueva necesaria: son lecturas (ficheros del repo) y un `git show` del commit
de la feature; ninguna ejecuta código del proyecto ni carga la máquina.

**Comandos.**

    git show --stat 60aa819                  (tamaño de la feature: qué ficheros y cuántas líneas)
    git show 60aa819 -- phoson_cli/controller.py    (el diff de la ruta de checkpoints)

**Qué se revisa.** La feature de checkpoints durables, que es lo que el PR #279 aporta y lo único que
nadie ha revisado línea a línea todavía: §5 del journal ya cubrió `procutil.py`, `conftest.py` y
`tools/bash.py`, pero **no** el camino de escritura a disco en mitad del turno.
Preguntas concretas: ¿puede el checkpoint duplicar mensajes? ¿es atómica la escritura? ¿qué pasa si el
proceso muere entre el append y el flush? ¿se respeta el contador monotónico frente a `_finalize_run` y
`_append_partial_history`?

**RESULTADOS de la revisión (`controller.py` +95/-17).**

*Correcto, verificado leyendo, no por los tests:*

1. **El contador va en unidades del historial del engine, no del árbol.** Es la parte sutil y está bien:
   `_drop_env_context` filtra mensajes, así que las posiciones del árbol NO coinciden con las del
   engine; contar posiciones del engine es lo único que mantiene la alineación. ✅
2. **Idempotencia real.** `_finalize_run` / `_append_partial_history` usan
   `max(counter, base_count)` como *piso*, no como origen del slice → los llamadores directos que no
   pasan por `_execute_turn` siguen funcionando. No hay duplicación (la pregunta del journal: **no
   puede duplicar**). ✅
3. **`_rebase_after_compaction` está guardado por `summarizer.pop_compact_events()`, que *consume* la
   bandera.** Como ahora se llama en cada checkpoint, el primer checkpoint tras una compactación hace el
   rebase y los siguientes entran por `if not compact_events: return False` → sin doble rebase. ✅
4. **Sólo guarda si el árbol cambió** (`if self._mirror_history_into_tree(...): await self._save_session()`)
   → los eventos que no añaden nada no pagan E/S. Buen diseño. ✅
5. **La escritura es genuinamente atómica** (`_save_sync`: `<file>.tmp.<pid>` + fsync + `os.replace`) y
   fuera del event loop (`asyncio.to_thread`). Un checkpoint interrumpido a mitad de escritura **no**
   puede corromper la sesión existente: es la piedra angular de la feature y se sostiene. ✅
6. El primer checkpoint (`await self._save_session()` antes de la primera llamada al LLM) va **después**
   de `_set_run_status(STATUS_ACTIVE, run_id)` → en disco quedan `status=active` + `run_id`. ✅

*Hallazgos (ninguno bloquea el merge):*

- **F1 (bajo, endurecer con 1 línea — es el único agujero real).** En `_mirror_history_into_tree` el
  contador se adelanta **antes** del append:

      start = self._synced_history_count
      self._synced_history_count = len(history)   # ← se adelanta aquí
      new_messages = _drop_env_context(history[start:])
      if not new_messages: return False
      created = self.tree.append_many(...)        # ← si esto lanza, la cola se perdió

  Si `append_many` lanza, el contador ya afirma que esos mensajes están espejados: quedan fuera del
  árbol **y de todos los reintentos**. Es el único camino donde esta feature es *menos* duradera que el
  código anterior (que reintentaba por slice). Probabilidad baja (haría falta OOM o un bug en
  `append_many`), pero en una feature de durabilidad el orden debería ser *append y luego avanzar*
  (el caso filtrado por `_drop_env_context` puede seguir avanzando: ahí no hay nada que perder).
- **F2 (rendimiento, no bug).** Cada checkpoint cuesta `save()` completo **más** `save_meta()`, que es
  `load()` (parseo completo) + `save()` otra vez: ~2 escrituras + 1 lectura del fichero entero por paso.
  El commit lo tarifa a 200 mensajes / 112 KiB = 41 ms. Pero el coste es O(pasos × tamaño): un turno de
  40 pasos sobre una sesión de 1000 mensajes son segundos de E/S por turno. Arreglo barato: el
  controlador ya tiene el árbol en memoria, así que `save_meta` no necesita releer
  (`tree.update_session_meta(...)` + un único `save()`).
- **F3 (cosmético).** Los checkpoints no refrescan `_context_tokens` (sólo `_finalize_run`): el contador
  de tokens de la cabecera va por detrás del estado en disco durante el turno. Defendible.
- **F4 (correcto, sólo se anota).** El rebase por compactación ahora ocurre *a mitad del stream* en vez
  de al cierre: si el proceso muere justo después del injerto, en disco queda la rama compactada como
  cursor y `status=active` → el resume continúa desde ahí. Es el comportamiento deseado.

**Cierre de §5 del journal (verificado en el árbol actual, post-#274):** `procutil.kill_process_tree`
pasa por `_kill_by_pid_refusal` (pid_alive + `windows_process_image` + imágenes críticas) y el branch
POSIX hace `pid_alive` antes del `getpgid` → los 3 agujeros de §5.1 cerrados, y el docstring de
`session_kwargs` ya dice la verdad (CPython ignora `start_new_session` en Windows). `bash.py:97` salta el
tree-kill si `proc.returncode is not None`. `bgjobs` importa de `procutil` y ya no lleva copias (§5.4).
Todo el entregable de §5 está cerrado.

---

## Entrada #8 — el cuelgue de CI **se reproduce**, y no es el test que acotamos

**Medición (baseline contra la que se compara todo).**

| run | rama | job `tests` | resultado |
|---|---|---|---|
| 38178940921 | `main` (`b013c48`) | 22:04:26 → 22:05:45Z = **79 s** | ✅ success |
| 38181956118 | PR #279 (`60aa819`) | mudo a los 14 s (18%) | ❌ cancelado a mano a los 29 min |
| 38189409096 | PR #279 (`448b86f`) | step iniciado 18:48:39 local; a las 18:51:57 llevaba **3 min 18 s** | ⏳ in_progress |

**Contra 79 s, llevar 3.5 min es ~2.7× el job completo ⇒ el cuelgue sigue ahí.**

**Y descarta HANG-1.** Si el cuelgue fuera `test_cancel_mid_turn_does_not_duplicate_checkpointed_steps`,
el `asyncio.wait_for(task, timeout=_CANCEL_GRACE)` que acabo de añadir lo habría hecho **fallar en 30 s**
y el run estaría cerrado hace rato. No es ese test: está en otro punto de la rama. Los dos parches de la
entrada #6 siguen siendo correctos por sí mismos (sin ellos el cuelgue es indefinido), pero **no arreglan
este cuelgue**.

**Lo que se ejecuta ahora (espera pasiva, sin carga):**

    powershell -NoProfile -Command "Start-Sleep -Seconds 420"

…y después `gh run view 38189409096 --json status,conclusion`. La espera es un `Start-Sleep` (no asigna
memoria, no arranca nada): sirve sólo para llegar al momento en que el log es descargable.

**Qué se espera, y por qué importa.**
- A los **10 min** de test, `-o faulthandler_timeout=600` vuelca la pila de todos los hilos ⇒ **el nombre
  del test atascado queda escrito en el log**.
- A los **20 min**, `timeout-minutes: 20` mata el job (lo de hoy: 29 min de silencio + cancelación manual).
- Con el job terminado, `gh run view 38189409096 --log` ya devuelve contenido (mientras corre, la API
  responde `BlobNotFound`, que es exactamente lo que contenía `_scratch_ci_live.log`).

**Hipótesis a batir cuando llegue el log.**
- **HANG-2**: cuelga un test **existente** cuyo comportamiento cambió con los checkpoints — el diff de
  `controller.py` mete `await self._save_session()` *dentro* de `run_turn`, así que cualquier test que
  llame a `run_turn` con un storage/engine simulados pasa ahora por `_ensure_session_title()` + dos E/S.
  Es la hipótesis más probable: el cuelgue sólo aparece en esta rama.
- **HANG-3**: cuelga un test nuevo del fichero de durabilidad distinto del de cancelación.
- Si el log nombra un test **sin relación** con la rama, entonces el cuelgue es del runner y HANG-0.

**RESULTADOS — el volcado llegó, y los guardas funcionaron.**

    tests Run tests 00:48:53  ................................ [ 18%]   ← último progreso
    tests Run tests 00:58:54  Timeout (0:10:00)!                        ← faulthandler_timeout=600
    tests Run tests 00:58:54  Thread 0x00007f9f6f419740 (most recent call first):
      selectors.py:468 in select
      asyncio/base_events.py:1961 in _run_once
      asyncio/base_events.py:645 in run_forever
      asyncio/runners.py:118 in run
      pytest_asyncio/plugin.py:716 in inner        ← un test async
      _pytest/python.py:166 in pytest_pyfunc_call
      _pytest/runner.py:... (marco de pytest normal)
    tests Run tests 01:08:30  ##[error]The operation was canceled.      ← timeout-minutes: 20

Del 22:50:21 al 23:19:38 del run anterior fueron 29 min de silencio y cancelación a mano; ahora: **10 min
+ volcado**, **20 min + corte automático**. El coste del cuelgue bajó a un tercio del de runner.

**Qué dice el volcado, y qué no.**
- El hilo principal está **ocioso dentro del selector del event loop**: es un test `async def` de
  `pytest_asyncio` esperando algo que **nunca se completa**. No es un subprocess colgado, ni un `sleep`
  bloqueante, ni un deadlock en C: el loop no tiene nada más que hacer.
- **HANG-1 queda descartada**: el `asyncio.wait_for(task, timeout=_CANCEL_GRACE)` habría hecho **fallar**
  el test de cancelación en 30 s. El run vivió 20 min, así que no es ese test.
- **HANG-2 (test existente afectado por el diff) gana peso**: el único cambio de producto de la rama son
  los checkpoints, y el punto 1 del diff mete un `await self._save_session()` **nuevo dentro de
  `run_turn`**, antes de la primera llamada al LLM. Cualquier test que llame a `run_turn` pasa ahora por
  `_ensure_session_title()` + dos E/S. `_ensure_session_title` es síncrono y barato (verificado: no
  programa el título LLM), así que el sospechoso es la E/S dentro de un bucle de test manipulado.
- **El volcado NO trae el nombre del test**: una corrutina suspendida no está en la pila nativa. Se
  necesita el nodeid.
- **El cuelgue es determinista**: 10 líneas × 72 = **720 tests completados** y la línea del 18% es la
  última en las **dos** corridas (22:50:21 y 00:48:53) ⇒ cuelga el **test #721**.

**Cómo se le pone nombre (propuesta, requiere autorización).** Que el CI imprima la lista ordenada de
nodeids alrededor del 721, y que el propio job nombre el test en vez de morir mudo:

```yaml
      # Temporal, diagnóstico del cuelgue de #279: imprime el orden real de
      # colección alrededor del test #721, que es el que se cuelga.
      - name: Locate the hanging test
        run: uv run pytest --collect-only -q | sed -n '700,745p'

      - name: Run tests
        # pytest-timeout como dep efímera (uv --with): no toca uv.lock.
        run: uv run --with pytest-timeout pytest -q --timeout=300 --timeout-method=thread -o faulthandler_timeout=600 || [ $? -eq 5 ]
```

- `--collect-only` no ejecuta nada: lista los nodeids en segundos.
- `--timeout=300` es seguro: la suite entera verde tarda **79 s**, ningún test legítimo llega a 300 s.
  Con él, un test atascado **se nombra** en el reporte y el job termina en ~6 min en vez de 20.
- El paso "Locate the hanging test" se quita en un commit posterior, ya con el nombre en la mano.

**Alternativa descartada.** Correr esto en Docker local: dos corridas de contenedor terminaron en
bugcheck (entradas #3 y #4). Azure no toca esta máquina.

---

## Entrada #9 — 2026-10-11 — El diagnóstico #8 corrió, y **corrige mi propia cuenta de índices**

**Qué se ejecutó (autorizado en #8):** el push de `8201766` disparó el run `38191017880` (headSha
`8201766`, job `tests`). Descarga del log, ya con el job terminado:

    gh run view 38191017880 --log > _scratch_ci_run3.log

Sólo lectura + escritura en `_scratch_*` (no es ejecución de tests en esta máquina: Azure corre los
tests, esta máquina sólo baja texto).

**Resultado del run:** `conclusion: failure`, y esta vez **en 5 min**, no en 20. El guarda hizo su
trabajo:

    01:13:28 .. 01:13:32  .... 10 líneas .... [ 18%]        ← 720 tests, igual que las dos corridas previas
    01:18:33  ..............................F.F+++ Timeout +++   ← 5 min después, pytest-timeout=300
    01:18:33  ~~~~~~~~~~~~~~~~~~~~ Stack of MainThread (140207874266944)
              selectors.py:468 in select
              asyncio/base_events.py:1961 in _run_once → 645 run_forever → runners.py:118 run
              pytest_asyncio/plugin.py:716 in inner → _pytest/python.py:166 → _pytest/runner.py
    01:18:33  ##[error]Process completed with exit code 1.
    01:18:33  Complete job  ← el job termina solo, sin llegar a los 20 min de `timeout-minutes`

**Tres cosas que cambian el diagnóstico de #8.**

1. **`pytest-timeout` sí funciona como dep efímera.** `uv run --with pytest-timeout …` instaló en 10 ms
   (`Installed 6 packages in 10ms`) y el timeout disparó a los 300 s con pila completa. Coste del
   cuelgue: 20 min → **5 min**. `uv.lock` intacto.
2. **La pila sigue sin nombrar el test.** Idéntica a la de #8: el MainThread está **ocioso dentro del
   selector del event loop** (`selectors.py:468`) esperando algo que nunca se completa, dentro de un
   `async def` de `pytest_asyncio` (marco `plugin.py:716`). Una corrutina suspendida **no está en la
   pila nativa**, así que ni faulthandler ni pytest-timeout pueden dar el nodeid. Se necesita `-v`.
3. **Mi cuenta del índice en #8 estaba mal.** Escribí "cuelga el test #721". Contando los dots de `-q`
   (72 por línea): las 10 líneas del 18% son los tests **1–720**, y el test que cuelga abre la línea
   *siguiente*. La diferencia entre run2 y run3 lo revela: en run2 la línea quedó **sin `\n`, sin
   flush**, y por eso no la vimos; en run3 el reporte del timeout **forzó el flush** y apareció:

       ..............................F.F
       └── 30 dots ──┘└F┘└d┘└F┘
         721…750      751 752 753  ← y el cuelgue es el siguiente, ~#754

   Es decir: **fallo en ~#751, pasa #752, fallo en ~#753 y cuelga ~#754**. Mi slice `sed -n '700,745p'`
   se quedó **9 tests corto** — imprimió exactamente la zona equivocada. Y el "test #721" era en
   realidad el primero de la línea, no el que cuelga.

**Dato nuevo y accionable: hay 2 fallos además del cuelgue.** El CI de `main` está **verde entero en
79 s** (baseline `38178940921`), así que `F.F` en esta rama apunta a **dos regresiones nuevas**
(compatibles con HANG-2: el diff de `controller.py` mete `await self._save_session()` *dentro* de
`run_turn`). Importante: el `-q` de las corridas 1 y 2 **nunca los mostró** porque la línea no se
flusheó y el job murió antes del resumen; sólo aparecen en run3 gracias al flush del timeout.

**Cambio propuesto (requiere autorización nueva: es otro push + otro run).** Que el job **nombre** el
test y muestre los dos fallos, en un solo run:

```yaml
      - name: Locate the hanging test
        run: uv run pytest --collect-only -q | sed -n '740,785p'   # antes 700,745 (9 corto)

      - name: Run tests
        run: uv run --with pytest-timeout pytest -v --timeout=300 --timeout-method=thread -o faulthandler_timeout=600 || [ $? -eq 5 ]
```

- **`-v` es el arreglo real, no el slice.** pytest imprime `tests/…::test_x ` **antes** de correrlo;
  el **último nodeid sin resultado** es el que cuelga. Los dos `F` también salen en línea, con su
  traceback, sin esperar al resumen final (que el timeout nunca deja llegar).
- El slice `740,785` queda sólo como confirmación cruzada del orden de colección.
- `--timeout=300` sigue siendo seguro: la suite verde tarda 79 s.

**Hipótesis a batir en el run siguiente.**
- **HANG-2** (mi favorita, ahora con `-v` se comprueba en un run): el que cuelga es un test
  **existente** que llama a `run_turn`, y los dos `F` son aserciones nuevas del checkpoint.
- **HANG-3**: cuelga un test **nuevo** de `test_hard_kill_durability.py` distinto del de cancelación.
  En contra: HANG-1 ya está descartada y ese fichero pasó 7/7 en local.
- **HANG-0** (colapso del runner): improbable — el baseline verde vive en el mismo runner.

**Nada ejecutado en local.** Sin pytest, sin Docker, sin contenedor. Todo el trabajo de esta entrada es
lectura de log de Azure + edición de `ci.yml` y esta bitácora.

---

## Entrada #10 — 2026-10-11 — **HANG-2 CONFIRMADA y causa raíz de los 4 resultados**

**Qué se ejecutó (autorizado en #9):** commit `78470f3`, push del branch, run `38191745319`
(headSha `78470f3`). Descarga:

    gh run view 38191745319 --log > _scratch_ci_run4.log

Otra vez `conclusion: failure` en ~5 min. **`-v` era el arreglo**: el run nombró todo.

**Los cuatro tests (contiguos, y los cuatro explican el `F.F` + cuelgue de #9):**

| # | test (todos en `tests/phoson_cli/fullscreen/test_fullscreen_shell_unit.py`) | resultado |
|---|---|---|
| ~751 | `test_ctrl_c_defers_exit_until_real_session_save_settles` | **FAILED** |
| ~752 | `test_deferred_exit_is_not_honored_when_real_session_save_fails` | PASSED |
| ~753 | `test_normal_save_failure_is_captured_and_rendered` | **FAILED** |
| ~754 | `test_escape_abort_save_failure_is_captured_and_rendered` | **HANG (timeout 300 s)** |

El nodeid del cuelgue lo dio el propio pytest-timeout, en la línea anterior al volcado:

    tests/phoson_cli/fullscreen/test_fullscreen_shell_unit.py::test_escape_abort_save_failure_is_captured_and_rendered +++ Timeout +++
    ~~~~~~~~~~~~~~~~~~~~ Stack of MainThread ~~~~~~~~~~~~~~~
      selectors.py:468 in select  ← loop ocioso, sin nada que despertarlo

**HANG-2 confirmada, y acotada a un solo fichero.** El fichero de tests es **byte a byte idéntico a
`origin/main`** (`git cat-file -e origin/main:…` = existe; `git diff --stat origin/main...HEAD --
tests/…` = sin cambios). Lo único que difiere en toda la rama es **`phoson_cli/controller.py`, +78/−17**.
Y el CI de `main` está verde entero (`38178940921`, 79 s): los 3 fallos + 1 cuelgue son **regresión pura
del diff de producto**, no flakiness. `HANG-1` y `HANG-3` quedan descartadas, y también `HANG-0`.

**Causa raíz (una sola, mecánica).** El diff añade un checkpoint **antes de la primera llamada al LLM**:

    self._set_run_status(STATUS_ACTIVE, run_id)        # controller.py:1480
    try:
        await self._save_session()                      # ← NUEVO, controller.py:1487
        …
        await self._refresh_context_window()            # controller.py:1522
        terminal_event = await self._consume_stream(path, config)   # 1525

…y el front-end fullscreen decide **dos cosas** con una bandera que sólo se activa al ver un evento
**terminal** (`sink.py:635` en `AgentDoneEvent`, `sink.py:651` en `AgentErrorEvent`, más
`_abort_turn` → `protect_persistence`, `controller.py:1575`):

    self._operation.persistence_required = True   # app.py:881-884, vía sink.on_persistence_required

Esa bandera gobierna (a) el mensaje al fallar, `app.py:855-860`:

    "Could not save session; the application remains open: …"  si persistence_required
    "Operation failed: …"                                    si no

y (b) el significado de Ctrl+C, `app.py:892-895`: con la bandera en True, `_cancel_operation` **no
cancela**: pone `exit_when_done` y devuelve `"protected"` (⇒ `lifecycle_controller.py:23-24` imprime
**"Saving session before exit..."**).

Con el checkpoint nuevo, el **primer** save de un turno simple pasa a ocurrir **antes** del evento
terminal. De ahí, exactamente, los cuatro resultados:

1. **#754 cuelga** — `test_escape_abort_save_failure…` para el flow con un monkeypatch real:
   `_refresh_context_window` → `refresh_started.set()` + `await asyncio.sleep(30)`, y `storage.save` →
   `raise OSError("abort save failed")`. El test hace `_trigger(app,"enter")` y luego
   `await refresh_started.wait()` **sin cota** (línea 1093). En la rama el save falla **antes** de que
   se llame a `_refresh_context_window`, la corrutina del turno muere con el `OSError`,
   `refresh_started` **nunca se setea** y la espera es infinita: el MainThread queda **ocioso en el
   selector** — la pila del timeout, literal. En `main` el orden es al revés (el save es el terminal),
   así que refresh sí corre y el escape cancela a tiempo: verde.
2. **#753 falla** — `failing_save` lanza `OSError("read-only filesystem")` en el **primer** save, que
   ahora es el pre-LLM ⇒ el sink no ha visto ningún evento terminal ⇒ `persistence_required` sigue
   **False** ⇒ el mensaje es `"Operation failed: read-only filesystem"` y el assert
   `"Could not save session" in _transcript(app)` (línea 1073) falla. `result.error` sí es el `OSError`
   correcto: por eso sólo salta ese assert.
3. **#751 falla** — `paused_save` hace `saving.set()` y espera `release_save`. El test hace
   `await saving.wait()` y **después** `_trigger(app,"c-c")`. En `main` ese save pausado es el
   **terminal** (`persistence_required` ya True) ⇒ c-c → `"protected"` ⇒ `"Saving session before
   exit..."` presente (assert 1015) y `mock_exit.assert_not_called()` ✓. En la rama el save pausado es
   el **pre-LLM** ⇒ la bandera sigue False ⇒ `_cancel_operation` **cancela de verdad** ⇒ no se imprime
   "Saving session before exit" ⇒ assert 1015 falla. `release_save` nunca se setea en esa rama del
   test, así que falla rápido (0.2 s), no cuelga.
4. **#752 pasa por casualidad** — sus asserts son todos del camino de error (`result.error` es OSError,
   `task.exception()` None, "Could not save session", `mock_exit` no llamado) y ese camino sobrevive al
   reordenamiento: el primer save pausado no cambia el tipo ni el receptor del error.

**Por qué no saltó en local y sí en CI.** El subconjunto que corrí en local fueron ficheros
"afectados" por el diff; este fichero no está entre ellos, y su fallo depende de **dónde cae el save
pausado** respecto al evento terminal, que es orden puro de la corrutina. CI corre la suite completa.

**Ya descartado como arreglo (comprobado en código, no ejecutado):** marcar `persistence_required` al
**arrancar el turno**. `_cancel_operation` con la bandera en True **no cancela y no llama a
`repl.cancel_current()`**, y `test_ctrl_c_cancels_instead_of_exiting_while_content_is_visible`
(línea 891) exige justo lo contrario: `mock_cancel.assert_called_once()`. Sería cambiar el
comportamiento de Ctrl+C en todo el turno para arreglar un test.

**Opciones de arreglo (decisión del diseño de la WIP, no la tomo yo).**

| | arreglo | #751 | #753 | #754 | coste |
|---|---|---|---|---|---|
| **A** | 1er checkpoint **después** de `_refresh_context_window()`, antes de `_consume_stream()` | ✗ | ✓ si además se protege la bandera al fallar el save | ✓ | pierde durabilidad durante la preparación (git env + refresh) |
| **B** | mantener el orden, **proteger la ventana del save** (protect→save→unprotect) y **proteger+re-lanzar** al fallar | ✓ | ✓ | ✗ cuelga igual | toca la semántica de la bandera en `app.py` |
| **C** | mantener el orden y **actualizar los 3 tests** a la semántica nueva | ✓ | ✓ | requiere cambiar el monkeypatch (que el **1er** save tenga éxito y falle el del **abort**) | toca tests de `main`; máxima durabilidad |

**Nota para cualquier opción:** `#754` cuelga **sin cota** por diseño del test
(`await refresh_started.wait()`), así que hay que arreglarlo sí o sí — un cuelgue de 300 s por test es
peor que un fallo, y ese patrón puede repetirse en otros tests con gates `asyncio.Event`.

**Nada ejecutado en local.** Sin pytest, sin Docker, sin contenedor. Todo es lectura de logs de Azure +
`git` de sólo lectura + edición de esta bitácora.

---

## Entrada #11 — 2026-10-11 — **Decisión: opción C** + diseño final del arreglo

**Decisión del owner (esta sesión):** hacer la **C** — asumir la semántica nueva: un save durabilidad-
crítico que falla es un **fallo de persistencia**, y una cancelación que cae **dentro** de ese save
**difiere la salida** en vez de interrumpirlo.

**Hallazgo que abarata la C: no hay que tocar 3 tests, sólo 1.** Al auditar los tests que dependen de
esta semántica apareció uno que **pasó** en CI y que fija el diseño:

    tests/phoson_cli/fullscreen/test_fullscreen_shell_unit.py:937
    test_cancel_during_context_refresh_persists_user_turn_to_disk        → PASSED [19%]

Ese test pausa `_refresh_context_window`, manda escape y exige que el turno **se cancele de verdad**
(`status == ABORTED`, `"Partial progress saved"`). Como el checkpoint pre-turno **ya ha corrido** en ese
punto, queda demostrado que la protección **no puede ser pegajosa desde el inicio del turno**: tiene que
**liberarse** cuando el save ha aterrizado, o el escape dejaría de cancelar y el turno seguiría hasta el
final. Eso descarta el arreglo "proteger al arrancar el turno" (que además ya había quedado descartado
por `test_ctrl_c_cancels_instead_of_exiting_while_content_is_visible:891`, que exige
`mock_cancel.assert_called_once()`).

Con el diseño correcto —**proteger sólo la ventana del save**— el recuento real es:

| test | con la C |
|---|---|
| `test_ctrl_c_defers_exit_until_real_session_save_settles` (#751) | **pasa sin cambios**: el c-c cae dentro de la ventana protegida |
| `test_deferred_exit_is_not_honored_when_real_session_save_fails` (#752) | pasa sin cambios |
| `test_normal_save_failure_is_captured_and_rendered` (#753) | **pasa sin cambios**: protegido antes del save ⇒ "Could not save session" |
| `test_escape_reports_persistence_is_protected` (línea 1106, **nunca llegó a correr**: el run murió en #754) | **pasa sin cambios**: mismo caso que #751, y `escape_controller.py:95` ya imprime "required persistence cannot be cancelled" con `"protected"` |
| `test_escape_abort_save_failure_is_captured_and_rendered` (#754) | **único cambio**: el save que falla debe ser el del **abort**, no el del checkpoint |

**Diseño implementado (4 ficheros).**

1. `phoson_cli/fullscreen/sink.py` — parámetro `on_persistence_released` y método
   `release_persistence()`, pareja de `protect_persistence()`.
2. `phoson_cli/fullscreen/app.py` — `_release_operation_persistence()` (pone
   `_operation.persistence_required = False`) y lo enlaza al sink. **No** toca `exit_when_done`: una
   salida ya pedida dentro de la ventana sigue vigente para cuando el turno se asiente.
3. `phoson_cli/controller.py` — nuevo `_checkpoint()`:

       protect_persistence()  →  await self._save_session()  →  release_persistence()

   usado en los **dos** puntos durabilidad-críticos: el checkpoint pre-LLM y el de cada
   `AgentToolStartEvent`/`AgentStepDoneEvent`. Un fallo **no** libera (la bandera queda puesta: el turno
   nunca llegó a disco). Los dos callbacks van con `getattr`/`callable`, igual que `_abort_turn`, porque
   `ClassicSink` (REPL clásico y tests) no los tiene.
4. `tests/…/test_fullscreen_shell_unit.py` — el checkpoint aterriza y el fallo se inyecta en el save
   siguiente (el del abort), con un contador `nonlocal`. Se añade `assert saves >= 2` para que el test
   no pueda volver a pasar "por accidente" sin ejercitar el save del abort.

**Auditoría de los demás `_save_session()`** (para que ningún fallo cambie de mensaje): error-event →
protegido por el sink (`AgentErrorEvent`), completado → `AgentDoneEvent`, abort → `protect_persistence()`
al entrar. `compact_context` (línea ~2128) y `_repair_orphaned_run` (usa `storage.save` directo) quedan
como estaban, sin envolver: no son rutas de turno.

**Comandos a ejecutar ahora (linter, no tests — sin pytest en el host):**

    uv run ruff check phoson_cli/controller.py phoson_cli/fullscreen/sink.py phoson_cli/fullscreen/app.py tests/phoson_cli/fullscreen/test_fullscreen_shell_unit.py
    uv run ruff format --check phoson_cli/controller.py phoson_cli/fullscreen/sink.py phoson_cli/fullscreen/app.py tests/phoson_cli/fullscreen/test_fullscreen_shell_unit.py

**Lo que sigue y requiere autorización aparte:** commit + push + un run de CI completo (Azure). Es la
**única** verificación posible de este arreglo en esta máquina: la suite no se puede correr en local por
los BSOD. Ese run además destapará cualquier test afectado que haya quedado **después del índice ~754**
(el run anterior murió ahí y no ejecutó el resto del fichero ni la cola de la suite).

**Riesgo residual documentado.** Si un c-c cae dentro de una ventana de save, se difiere la salida y el
turno **no** se cancela: sigue hasta su final natural y el app sale al terminar. Es lo que exige #751 y
el precio de no interrumpir una escritura obligatoria; el hueco es el de un `save` (milisegundos salvo
que el disco esté lento). Queda como comportamiento consciente, no accidental.

**RESULTADOS de los comandos de arriba.**

    uv run ruff check  …  →  All checks passed!
    uv run ruff format --check  …  →  4 files already formatted

**Dos correcciones más, encontradas al revisar el propio diff.**

1. **Guardia `terminal is None`** en el checkpoint del stream
   (`controller.py:979`). Sin ella, si un motor mal educado emitiera un paso *después* del evento
   terminal, el `release` del checkpoint borraría el pestillo que el propio evento terminal acaba de
   poner — y el fallo del save de fin de turno volvería a reportarse como "Operation failed". Con la
   guardia, tras el terminal ya no se hace ningún checkpoint. Es un no-op para el contrato real del
   engine ("exactamente un evento terminal") y deja el invariante escrito al lado del código.
2. **El test no se ata a un ordinal.** La primera versión del arreglo de #754 fallaba el save
   "número 1"; lo cambié a **"falla todo save que ocurra después de que el refresh haya empezado"**
   (`if not refresh_started.is_set()`). Es semántico —"el save del abort es el que falla"— en vez de
   depender de cuántos saves haga hoy el camino, así que no se rompe si mañana aparece un save nuevo
   antes. Se conserva `assert saves >= 2` para que el test no pueda volver a pasar sin ejercitar
   realmente el save del abort.

**Estado del cambio (5 ficheros, +263/−4 incluyendo bitácora):**

| fichero | cambio |
|---|---|
| `phoson_cli/controller.py` | `_checkpoint()` (protect→save→release), 2 usos, guardia `terminal is None` |
| `phoson_cli/fullscreen/sink.py` | `on_persistence_released` + `release_persistence()` |
| `phoson_cli/fullscreen/app.py` | `_release_operation_persistence()` + wiring |
| `tests/…/test_fullscreen_shell_unit.py` | sólo `test_escape_abort_save_failure_is_captured_and_rendered` |
| `BITACORA.md` | entradas #9, #10, #11 |

**Sin commitear todavía, sin push, sin CI.** El siguiente paso (commit + push + run completo en Azure)
necesita autorización explícita: es la **única** verificación posible en esta máquina, y además es la que
revelará si queda algún test afectado **después del índice ~754**, que el run anterior nunca llegó a
ejecutar.

---

## 12. Plan de commit + push + run de CI (2026-10-11)

**Estado:** comandos **escritos aquí antes de ejecutarlos**, aún **no** autorizados ni corridos. Nada de
esto se ejecuta hasta que el owner dé el visto bueno. Se añaden sólo los **cinco** ficheros del arreglo;
los `_scratch_*`, `BSOD_INVESTIGATION.md` y `tests/_bsod_journal.py` son forense local y **se quedan
fuera** (nunca `git add -A`).

**Mensaje de commit (ASCII, sin acentos; tipo `fix(cli)` como los recientes):**

    fix(cli): protect mid-turn checkpoints so a save failure reports as persistence

    The pre-LLM checkpoint runs before any terminal event, so the front end had
    not yet latched persistence_required: a save failure there was rendered as a
    generic "Operation failed", and a Ctrl+C landing inside the write cancelled
    the turn instead of deferring the exit until the write settled.

    Protect only the save window (protect -> save -> release) rather than latching
    from the start of the turn, so an ordinary mid-turn Ctrl+C still cancels. A
    failed checkpoint deliberately leaves the flag set: the turn never reached disk.

**Comandos, en orden:**

    git add BITACORA.md phoson_cli/controller.py phoson_cli/fullscreen/app.py phoson_cli/fullscreen/sink.py tests/phoson_cli/fullscreen/test_fullscreen_shell_unit.py
    git status --short
    git commit -m "fix(cli): protect mid-turn checkpoints so a save failure reports as persistence" -m "The pre-LLM checkpoint runs before any terminal event, so the front end had not yet latched persistence_required: a save failure there was rendered as a generic Operation failed, and a Ctrl+C landing inside the write cancelled the turn instead of deferring the exit until the write settled." -m "Protect only the save window (protect -> save -> release) rather than latching from the start of the turn, so an ordinary mid-turn Ctrl+C still cancels. A failed checkpoint deliberately leaves the flag set: the turn never reached disk."
    git push origin fix/mid-turn-durability

**Seguimiento del run (Azure).** Tras el push, localizar el run de esta rama y bajar su log:

    gh run list --branch fix/mid-turn-durability --limit 3
    gh run watch <run-id> --exit-status
    gh run view <run-id> --log > _scratch_ci_run5.log

(`gh run view --log` es sólo lectura; el log va a `_scratch_ci_run5.log`, que es untracked.)

**Criterios de verificación del run** (lo único que se puede comprobar en esta máquina):

1. `test_ctrl_c_defers_exit_until_real_session_save_settles` → PASSED.
2. `test_normal_save_failure_is_captured_and_rendered` → PASSED.
3. `test_escape_abort_save_failure_is_captured_and_rendered` → PASSED (ya no HANG; el job sigue y
   termina en lugar de morir en el `pytest-timeout` de 300 s).
4. El job `tests` **supera el ~18%** y **termina**, ejecutando por fin la cola que nunca corrió
   (resto de `test_fullscreen_shell_unit.py` y el resto de la suite). Cualquier fallo ahí es nuevo
   terreno y se anota como hallazgo, no se arregla a ciegas.

**Rollback si sale mal:** `git revert <sha>` (o simplemente no mergear PR #279). El commit **no** toca
la lógica del engine: `_checkpoint()` sólo envuelve el `_save_session()` que ya existía, así que revertir
devuelve el árbol al comportamiento de `78470f3`.

**Pendiente para un commit posterior (no urgente):** quitar el paso temporal `Locate the hanging test`
(`sed -n '740,785p'`) del workflow y decidir si se conservan `-v`, `--with pytest-timeout`,
`--timeout=300 --timeout-method=thread`, `-o faulthandler_timeout=600` y `timeout-minutes: 20`.