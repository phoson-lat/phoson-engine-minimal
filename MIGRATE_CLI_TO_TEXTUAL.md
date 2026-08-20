# Plan: migración gradual del CLI a Textual

## Objetivo

Migrar la experiencia interactiva de `phoson-cli` desde la combinación actual de `prompt_toolkit` + Rich hacia una TUI basada en **Textual**, sin afectar:

- El runtime de agentes (`phoson_agent` y `phoson_llm`).
- El modo no interactivo / one-shot (`phoson-cli -p "..."`, argumentos y stdin).
- La persistencia y el árbol de sesiones existente.
- La seguridad de `safe_mode`.

La migración debe ser progresiva: inicialmente Textual coexistirá con el REPL clásico y solo se volverá predeterminado cuando alcance paridad funcional.

---

## Estado actual y alcance

### Componentes a conservar

| Componente | Archivo(s) | Tratamiento |
|---|---|---|
| Motor, eventos y herramientas | `phoson_agent/`, `phoson_llm/` | Sin cambios de arquitectura. |
| Configuración y proveedores | `phoson_cli/config.py` | Compartido por ambas UI. |
| One-shot | `phoson_cli/__main__.py::_run_oneshot` | Sin cambio funcional. |
| Sesiones y árbol | `phoson_agent/sessions/`, `phoson_cli/_session.py` | Compartido por ambas UI. |
| Comandos y parser | `phoson_cli/commands.py` | Mantener la especificación y desacoplar las vistas. |
| Formateadores Rich | `phoson_cli/_views.py`, `phoson_cli/tools/subagent_panel.py` | Reutilizar renderables cuando sea apropiado. |

### Componentes a migrar o abstraer

| Componente | Archivo(s) | Motivo |
|---|---|---|
| Bucle interactivo | `phoson_cli/repl.py` | Acoplado a `PromptSession`, keybindings y renderer de consola. |
| Render de eventos | `phoson_cli/renderer.py` | Usa `Live`, escrituras directas a `console.file` y hilos; no es compatible dentro del loop de Textual. |
| Completado y entrada | `phoson_cli/repl.py` | `prompt_toolkit` debe reemplazarse por widgets y bindings Textual. |
| Pickers | `phoson_cli/pickers/`, `model_picker.py`, `provider_picker.py`, `session_picker.py` | Deben convertirse en pantallas/modales Textual. |
| Setup wizard | `phoson_cli/installer.py` | Actualmente usa `PromptSession`. |
| Confirmación de Bash | `phoson_cli/tools/bash.py` | No puede abrir un segundo prompt dentro de una aplicación Textual. |

---

## Principios de diseño

1. **Separar dominio de interfaz.** La ejecución, persistencia y estado de una conversación no deben depender de Rich, Textual ni `prompt_toolkit`.
2. **Un consumidor único por stream.** El stream de `AgentEvent` se procesa en una tarea async y las actualizaciones de interfaz se serializan en el event loop de Textual.
3. **No usar `Rich.Live`, `console.file` ni threads de animación en Textual.** Textual gestiona el render, los timers y el loop de eventos.
4. **Conservar compatibilidad durante la transición.** `--classic` debe ofrecer el REPL actual; Textual inicia bajo `--textual` hasta que se estabilice.
5. **No duplicar la lógica de comandos.** `COMMAND_SPECS` y `parse_command()` siguen siendo la fuente de verdad.
6. **La aprobación humana es una dependencia inyectable.** `safe_mode` debe solicitar confirmación mediante un contrato async, no directamente mediante `PromptSession` desde una tool.

---

## Fase 0 — Decisiones y base de dependencias ✅ (PR #39)

### Cambios

1. Agregar Textual como extra opcional en `pyproject.toml`:

   ```toml
   [project.optional-dependencies]
   tui = ["textual>=...<..."]
   ```

2. Añadir una guía de instalación y ejecución al README:

   ```bash
   uv sync --extra tui
   phoson-cli --textual
   ```

3. Añadir flags de UI en `phoson_cli/__main__.py`:
   - `--textual`: inicia la TUI nueva.
   - `--classic`: fuerza el REPL basado en `prompt_toolkit`.
   - En esta etapa no cambiar el modo por defecto.

4. Definir el comportamiento cuando Textual no está instalado: mensaje claro con instrucción para instalar el extra, sin traceback.

### Criterios de salida

- `uv sync` sin el extra continúa funcionando.
- `phoson-cli --textual` falla amigablemente si falta Textual.
- El modo one-shot no interpreta `--textual` como parte de la tarea.

---

## Fase 1 — Extraer un controlador independiente de UI ✅ (PR #40)

### Objetivo

Convertir `PhosonRepl` de `phoson_cli/repl.py` en una capa delgada de entrada/salida, moviendo la lógica reutilizable a un controlador sin dependencias de UI.

### Cambios

1. Crear `phoson_cli/controller.py` con, por ejemplo, `SessionController`.

2. Mover desde `PhosonRepl` al controlador:
   - Creación y reconstrucción de `chat`, `AgentEngine`, tools, middlewares y plugins (`_rebuild_engine`).
   - Cierre seguro de clientes y plugins (`close_plugins`).
   - Estado de sesión: `SessionState`, `ConversationTree`, cursor, métricas, adjuntos.
   - Creación y append de mensajes (`_build_user_message`, `_append_user_turn`).
   - Ejecución y persistencia de una interacción (`_consume_stream`, `_finalize_run`, `_append_partial_history`, `_persist_run_reasoning`).
   - Cambio de modelo/proveedor, sesiones, etiquetas y undo.
   - Context window y cálculo de tokens.
   - Construcción del system prompt.

3. El controlador debe exponer una API explícita, por ejemplo:

   ```python
   class SessionController:
       async def run_turn(self, text: str, sink: AgentEventSink) -> RunOutcome: ...
       async def load_session(self, session_id: str) -> LoadOutcome: ...
       async def shutdown(self) -> None: ...
       def new_session(self) -> None: ...
       def set_model(self, model: str) -> None: ...
       def set_provider(self, provider: str) -> None: ...
       def undo_last_turn(self) -> tuple[bool, str]: ...
   ```

4. Definir un protocolo para el destino de eventos, por ejemplo en `phoson_cli/ui_protocols.py`:

   ```python
   class AgentEventSink(Protocol):
       def on_event(self, event: AgentEvent) -> None: ...
       def on_user_message(self, message: Message) -> None: ...
       def on_run_cancelled(self) -> None: ...
       def take_reasoning(self) -> str: ...
   ```

5. Adaptar `PhosonRepl` para delegar al controlador, conservando su UX y API pública mientras dure la transición.

### Pruebas

- Extraer o ampliar pruebas unitarias para `SessionController`:
  - éxito, error y cancelación de una ejecución;
  - persistencia de progreso parcial;
  - cambio de modelo/proveedor y cierre de plugins;
  - carga de sesión, undo, ramas y etiquetas;
  - cálculo de métricas y contexto.
- Conservar `tests/phoson_cli/test_repl_unit.py` como prueba de compatibilidad del modo clásico.

### Criterios de salida

- El REPL clásico mantiene el comportamiento actual.
- El controlador puede ejecutar un turno usando un sink falso en pruebas, sin `prompt_toolkit`, Rich ni una TTY real.

---

## Fase 2 — Abstraer renderizado y confirmaciones ✅ (PR #41)

### 2A. Renderizado

1. Mantener `Renderer` como implementación clásica, idealmente renombrada a `ConsoleRenderer` o envuelta por ella.
2. Separar los formateadores puros de las operaciones de consola:
   - Paneles de reasoning.
   - Tablas de herramientas/costos.
   - Markdown y mensajes de error.
   - Formato de subagentes de `tools/subagent_panel.py`.
3. Encapsular estos puntos incompatibles con Textual:
   - `WaitingSpinner`.
   - `SubagentSpinner`.
   - `Live` de streaming.
   - `console.file.write()`.
4. En el backend clásico, conservar el comportamiento actual. En el backend Textual futuro, representar el mismo estado con widgets reactivos.

### 2B. Confirmación de safe mode

1. Crear un protocolo async, por ejemplo:

   ```python
   class ConfirmationService(Protocol):
       async def confirm_bash(self, command: str) -> bool: ...
   ```

2. Implementar `PromptToolkitConfirmationService` como fallback clásico.
3. Cambiar `phoson_cli/tools/bash.py`:
   - No crear directamente un `PromptSession` si hay una función de confirmación inyectada.
   - Mantener el comportamiento actual como fallback para compatibilidad.
4. Inyectar la función/servicio a través de `engine.context.extra` y ampliar la inyección de la tool si es necesario.
5. Definir comportamiento no interactivo: en `safe_mode`, rechazar la orden si no hay servicio de confirmación explícito.

### Pruebas

- Pruebas de Bash con un confirmador fake: aprobación, rechazo, excepción/cancelación y ausencia de UI.
- Pruebas de renderer clásico existentes: `tests/phoson_cli/test_renderer_unit.py`.

### Criterios de salida

- Ninguna tool requiere importar `prompt_toolkit` para funcionar cuando recibe un confirmador inyectado.
- La interfaz Textual podrá mostrar un modal sin competir por stdin.

---

## Fase 3 — TUI Textual mínima viable ✅ (PR #42)

> **Implementado (MVP):** paquete `phoson_cli/textual/` con `PhosonTextualApp`,
> `TextualSink` (AgentEventSink sobre widgets), `TextualConfirmationService`
> (modal `BashConfirmation` para safe_mode), widgets `UserTurn` /
> `StreamingTurn` (Markdown + `ReasoningView` Collapsible + `ToolCard`) /
> `StatusLine`, barra de estado y composer `Input`. Cada turno es una tarea
> async controlada por la app; Ctrl+C cancela (semántica idéntica al REPL:
> parcial persistido), Ctrl+T toggla reasoning (live o persistido), Ctrl+L
> limpia la vista, Ctrl+Q / `/exit` cierran con `controller.shutdown()`.
> Comandos TUI: `/help /new /tree /undo /label /env /cost /tokens /steps
> /model [id] /sessions [id] /exit` — los pickers interactivos se quedan en
> el REPL clásico (Fase 4). `--textual` lanza la app; el REPL clásico sigue
> siendo el default. Tests: `tests/phoson_cli/test_textual_tui.py`
> (headless con `App.run_test`).
> Pendiente de la fase (se mueve a Fase 4/6): Composer multilínea
> (Cmd+Enter), `SubagentStatusPanel` dedicado y `Ctrl+Enter`.
>
> **Correcciones post-MVP (PR #45):** (1) `StreamingTurn` heredaba
> `height: 1fr` + `overflow: hidden` de `Vertical` y recortaba cualquier
> respuesta más alta que el viewport (no había nada que scrollear) —
> ahora `height: auto` y la conversación crece; (2) auto-follow: el
> viewport sigue al fondo mientras streama (flag `_follow` + tick de 0.1 s
> que cubre la cola de render asíncrono de `Markdown`), se libera con
> rueda/PgUp y se re-arma al volver al fondo o en un mensaje nuevo;
> (3) `PgUp`/`PgDn` hacen scroll de página aunque el composer tenga el
> foco; (4) **bug de entrada en Kitty**: Textual 8.2.8 malinterpreta los
> reports de "associated text" de Kitty (cada tecla llegaba como
> `tecla + ';<dígitos>'` — imposible escribir `/help`), por lo que el
> TUI desactiva ese flag al arrancar (`_workaround_kitty_associated_text`
> en `__main__.py`, con test canary documentando el bug del parser);
> (5) diagnóstico de entrada: `PHOSON_TEXTUAL_DEBUG=1` loguea las teclas
> que llegan (el `Input` detiene la propagación de caracteres
> imprimibles, por eso el hook va en un subclase del composer) y
> `PHOSON_TEXTUAL_LEGACY_KEYS=1` fuerza secuencias xterm legacy
> (`TEXTUAL_DISABLE_KITTY_KEY`) como último recurso.

### Archivos nuevos

Crear un paquete específico, por ejemplo:

```text
phoson_cli/textual/
├── __init__.py
├── app.py
├── renderer.py
├── screens.py
├── widgets.py
└── dialogs.py
```

### Estructura propuesta

1. `PhosonTextualApp(App)`:
   - Recibe `PhosonConfig` y construye/reutiliza `SessionController`.
   - En `on_mount`, crea layout, foco de entrada y estado inicial.
   - En `on_shutdown`, cancela tareas y llama a `controller.shutdown()`.

2. Widgets iniciales:
   - `ConversationView`: scroll vertical de turnos, Markdown, herramientas y errores.
   - `StreamingTurn`: widget actualizable para reasoning y contenido del turno activo.
   - `Composer`: `Input` multilínea o widget personalizado para enviar mensajes.
   - `StatusBar`: modelo, proveedor, id de sesión, tokens, costo, estado de ejecución y adjuntos.
   - `SubagentStatusPanel`: estado persistente de subagentes mientras están activos.

3. `TextualRenderer` / `TextualEventSink`:
   - Convierte cada `AgentEvent` en actualización de widgets.
   - Acumula contenido y reasoning del turno activo.
   - No utiliza `Live`, threads ni escritura directa a stdout.

4. Ejecutar cada turno con `self.run_worker(..., exclusive=False)` o una tarea async controlada. La cancelación debe conservar la semántica actual:
   - cancelar `current_task`;
   - persistir historial parcial;
   - mostrar “Partial progress saved.”;
   - devolver el foco al compositor.

5. Atajos mínimos:
   - `Ctrl+Enter` o `Enter`: enviar, según el diseño de input elegido.
   - `Ctrl+C`: cancelar la ejecución activa; si no existe, pedir salida o salir.
   - `Ctrl+T`: ocultar/mostrar reasoning activo; fuera de una ejecución, abrir reasoning persistido.
   - `Ctrl+L`: limpiar visualmente el panel, sin borrar la sesión.
   - `Escape`: cerrar modal o cancelar selector.

6. Comandos slash:
   - Reutilizar `parse_command()` y `CommandHandler` donde sea posible.
   - Crear un adaptador de vistas para que los comandos no dependan de `Renderer.console`.
   - Prioridad de soporte: `/help`, `/new`, `/tree`, `/undo`, `/env`, `/cost`, `/tokens`, `/steps`, `/attach`, `/label`, `/sessions`, `/model`, `/provider`, `/exit`.

### Diseño de streaming

- Mantener un solo `StreamingTurn` por ejecución.
- `AgentTokenEvent`: actualizar el bloque de respuesta.
- `AgentReasoningEvent`: actualizar un `Collapsible` de reasoning.
- Eventos de herramientas: añadir o actualizar una tarjeta con nombre, argumentos resumidos y resultado.
- `AgentDoneEvent`: fijar el turno como histórico, refrescar status bar y scroll al final si el usuario no se desplazó manualmente.
- `AgentErrorEvent`: fijar una tarjeta de error sin destruir el contenido recibido antes del error.

### Criterios de salida

Con `phoson-cli --textual`, el usuario puede:

- enviar mensajes y recibir streaming;
- ejecutar herramientas;
- cancelar un turno;
- ver errores y costos;
- alternar reasoning;
- crear y persistir sesiones;
- usar los comandos básicos y salir limpiamente;
- ejecutar comandos bash en `safe_mode` mediante un modal de confirmación.

El modo clásico y one-shot siguen pasando sus pruebas.

---

## Fase 4 — Diálogos, selección y árbol navegable

### Cambios

1. Reemplazar gradualmente los pickers de `prompt_toolkit` con modales Textual:
   - `ModelPickerScreen`: búsqueda incremental, precio, contexto y modelo activo.
   - `ProviderPickerScreen`: proveedores configurados y estado de credenciales.
   - `SessionPickerScreen`: lista, filtro, carga y eliminación con confirmación.
   - `AttachmentDialog`: selector de ruta y lista de adjuntos pendientes.

2. Añadir un panel o pantalla de árbol de conversaciones:
   - usar `ConversationTree` como modelo de datos;
   - mostrar nodo activo y ramas abandonadas;
   - permitir navegar a un nodo y continuar desde allí;
   - conservar `/tree` como representación textual y fallback.

3. Llevar `/model`, `/provider` y `/sessions` a estos diálogos cuando se ejecuten en la TUI.

4. Hacer que los comandos expongan solicitudes de UI en vez de importar directamente `pick_model`, `pick_provider` o `PromptSession`.

### Criterios de salida

- Las selecciones de modelo, proveedor y sesiones funcionan sin `prompt_toolkit` en modo Textual.
- Se puede inspeccionar y cambiar de rama desde la TUI.
- Los comandos siguen funcionando en modo clásico con sus pickers existentes.

---

## Fase 5 — Wizard de configuración y paridad

### Cambios

1. Crear `TextualSetupWizard` como flujo de pantallas o pasos:
   - proveedores habilitados;
   - credenciales;
   - modelo por defecto;
   - opciones de ejecución (safe mode, subagentes, MCP, tema);
   - resumen y confirmación de guardado.

2. Mantener `run_install_wizard()` clásico durante una versión de compatibilidad.
3. Hacer que `--setup --textual` lance el wizard Textual si la dependencia está presente.
4. Mantener una opción no interactiva/documentada para entornos CI.

### Criterios de salida

- Una instalación nueva puede configurarse completamente desde Textual.
- El wizard conserva validaciones y formato de `PhosonConfig` existentes.

---

## Fase 6 — Calidad, accesibilidad y cambio de predeterminado

### Pruebas

1. Añadir pruebas de Textual con `App.run_test()` para:
   - montaje de la app;
   - envío de mensaje y render de token/evento;
   - cancelación;
   - apertura/cierre de modal de safe mode;
   - selectores;
   - atajos, en especial `Ctrl+T`;
   - cierre y liberación de plugins.

2. Mantener pruebas de lógica del controlador independientes de UI.
3. Mantener pruebas del REPL clásico hasta retirarlo explícitamente.
4. Ejecutar además:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run pyright
   uv run pytest
   ```

### Observabilidad y UX

- Verificar que el costo, tokens y métricas por step se actualizan igual que en consola.
- Asegurar que errores de autenticación conservan el mensaje de recuperación (`/setup` o configuración de key).
- Soportar terminales sin color, terminales estrechas y pegado de texto largo.
- Asegurar que el scroll no salta al final si el usuario está revisando mensajes anteriores.

### Cambio de predeterminado

Solo después de una versión con `--textual` estable:

1. Hacer Textual el modo interactivo predeterminado cuando exista TTY.
2. Dejar `--classic` durante una o dos versiones menores.
3. Recoger y resolver regresiones de compatibilidad.
4. Eliminar `prompt_toolkit` y el REPL clásico únicamente si no quedan consumidores (incluidos pickers, wizard y confirmaciones).

---

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Uso de `Rich.Live` o escritura directa a stdout dentro de Textual | Corrupción visual o bloqueo | Sustituir por widgets, reactivos y timers Textual. |
| Prompt de `safe_mode` desde una tool | Competencia por stdin / UI bloqueada | Servicio de confirmación async inyectable y modal Textual. |
| Eventos de stream concurrentes con modificaciones de widgets | Estado inconsistente | Un único consumidor de stream y actualizaciones en el message loop. |
| Fugas de conexiones MCP o clientes LLM al cerrar la app | Procesos/conexiones huérfanos | `on_shutdown` llama a `SessionController.shutdown()` y prueba explícita de cierre. |
| Reescritura completa demasiado grande | Regresiones y retrasos | Flags `--textual`/`--classic`, migración por fases y paridad progresiva. |
| Duplicación de comandos entre ambas UIs | Comportamiento divergente | Conservar `COMMAND_SPECS`/parser y usar adaptadores de UI. |

---

## Estimación orientativa

| Fase | Esfuerzo |
|---|---:|
| 0–1: dependencia, flags y controlador | 3–5 días |
| 2: renderer y confirmaciones desacopladas | 2–4 días |
| 3: MVP Textual | 5–8 días |
| 4: pickers, árbol y adjuntos | 4–7 días |
| 5: wizard | 2–4 días |
| 6: pruebas, hardening y transición | 3–5 días |

**Total aproximado:** 4–7 semanas de trabajo secuencial, o menos si se ejecutan en paralelo partes bien aisladas (controller/confirmaciones, widgets/pickers y pruebas).

---

## Definición de terminado

La migración se considera completa cuando:

- Textual es la interfaz interactiva predeterminada.
- El modo one-shot conserva exactamente su uso y códigos de salida.
- La TUI soporta streaming, tools, subagentes, reasoning, safe mode, sesiones, ramas, modelo, proveedor, adjuntos y configuración.
- No existen usos productivos de `prompt_toolkit`, `Rich.Live`, hilos de spinner ni escrituras directas a consola dentro de la ruta Textual.
- Los plugins y clientes se cierran correctamente al salir, cambiar proveedor/modelo o cancelar.
- La suite de pruebas cubre controlador, TUI y compatibilidad no interactiva.
