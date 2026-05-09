# TODO tecnico: phoson_llm y phoson_agent

Este backlog recoge las observaciones de calidad, estructura, estilo Python, mantenibilidad y escalabilidad detectadas en `phoson_llm` y `phoson_agent`. Las tareas estan priorizadas por impacto tecnico y riesgo de crecimiento.

## Estado despues de la primera ronda de cambios

Verificacion realizada despues de los cambios actuales:

- `python -m ruff check phoson_llm phoson_agent phoson_cli phoson_plugin_mcp tests/phoson_llm tests/phoson_agent tests/phoson_cli tests/phoson_plugin_mcp`: pasa.
- `python -m pytest tests/phoson_llm tests/phoson_agent tests/phoson_cli tests/phoson_plugin_mcp`: `202 passed`, `13 skipped`, `1 warning`.
- `pyright` ya esta agregado a dependencias de desarrollo, pero falta correrlo despues de sincronizar el entorno.

Tareas ya avanzadas o cerradas parcialmente:

- Contrato de `ToolHandler`: se eligio exigir `(args, context)` y se actualizo el tipo principal.
- `SummarizationMiddleware`: se removio la compaccion falsa en `on_before_llm`; ahora la compaccion real vive en `wrap_llm_call`.
- Ruff: los errores detectados originalmente ya estan corregidos.
- Docs de `phoson_llm` y `phoson_agent`: se corrigieron varios ejemplos desalineados (`build_chat`, `PriceEntry`, `AgentContext`, `RetryMiddleware`, `JsonlStorage`).
- Helpers OpenAI-compatible: se creo `phoson_llm/chats/_openai_compatible.py` y se reutiliza desde `OpenAIChat` y `OpenRouterChat`.

Hallazgos nuevos despues de revisar los cambios:

- `AgentEngine` ya no tiene helpers duplicados; solo se extrajo `_get_tool_definitions` como refactor pequeno.
- `SummarizationMiddleware` perdio tests de compaccion falsa, pero aun falta agregar tests nuevos que validen la ruta real de `wrap_llm_call`.
- Pyright esta configurado en `pyproject.toml` y agregado a dev dependencies; falta ejecutar el check despues de sincronizar dependencias.
- El helper `_openai_compatible.py` reduce duplicacion, pero aun usa `dict` genericos y no extrae la acumulacion de tool calls.

## Prioridad alta

### 1. Corregir el contrato de `ToolHandler` vs la ejecucion real del engine

**Estado:** mayormente resuelto; falta cerrar documentacion/pruebas explicitas.

**Archivos involucrados:**

- `phoson_agent/models.py`
- `phoson_agent/agent.py`
- `phoson_agent/main.py`
- `tests/phoson_agent/*`

**Problema:**

`ToolHandler` permite dos formas de handler: una funcion que recibe solo `args` y una funcion que recibe `args` mas `AgentContext`. Sin embargo, `AgentEngine` llama siempre `tool.handler(call.args, self.context)`. Esto contradice el tipo declarado y puede romper herramientas creadas manualmente que solo aceptan un argumento.

Un ejemplo de esta inconsistencia existe en el demo de `phoson_agent/main.py`, donde `get_weather(args)` recibe un solo argumento, pero el engine lo ejecutaria con dos.

**Tarea:**

- Hecho: se decidio exigir `(args, context)` como contrato oficial.
- Hecho: `ToolHandler` ahora declara `Callable[[dict[str, Any], "AgentContext"], ...]`.
- Hecho: el demo `get_weather` fue ajustado para aceptar `context`.
- Pendiente: revisar documentacion publica para que el contrato de handlers manuales quede explicito.
- Pendiente: agregar una prueba especifica que confirme que un `AgentTool` manual debe aceptar `(args, context)` y que un handler de un solo argumento ya no es contrato soportado.
- Pendiente: validar que plugins externos o ejemplos existentes no sigan registrando handlers manuales de un argumento.

**Impacto:**

- Reduce bugs en runtime al registrar tools manuales.
- Hace la API publica mas confiable.
- Evita que integradores externos tengan que leer el engine para saber la firma real esperada.

**Riesgo si no se atiende:**

- Herramientas validas segun el tipo pueden fallar en ejecucion.
- La documentacion y el sistema de tipos daran una falsa sensacion de seguridad.

---

### 2. Simplificar y corregir `SummarizationMiddleware`

**Estado:** parcialmente resuelto; falta fortalecer tests.

**Archivos involucrados:**

- `phoson_agent/plugins/summarizer.py`
- `tests/phoson_agent/test_summarizer.py`

**Problema:**

`SummarizationMiddleware` tiene dos rutas de compaccion: `on_before_llm` y `wrap_llm_call`. El engine ejecuta `on_before_llm` antes del wrapper, por lo que los mensajes pueden ser compactados primero con un placeholder y despues procesados otra vez por el wrapper.

Ademas, `_compact_messages` construye un mensaje de resumen con el prompt completo de resumen, no con un resumen generado por el modelo. Esto puede terminar enviando al modelo algo como `[Conversation summary: <prompt para resumir>]` en lugar de un resumen real.

**Tarea:**

- Hecho: `on_before_llm` ya no muta mensajes.
- Hecho: se removio `_compact_messages`, que generaba pseudo-resumenes.
- Hecho: la compaccion real se concentra en `wrap_llm_call`.
- Pendiente: agregar pruebas nuevas para `wrap_llm_call` que validen que se genera un resumen real via LLM simulado.
- Pendiente: agregar prueba que asegure que el mensaje compactado no contiene el prompt completo de resumen.
- Pendiente: agregar prueba que asegure que no hay doble compaccion.
- Pendiente: validar que `SummarizationEvent` se pueda consumir de forma clara; actualmente se acumula internamente y se obtiene con `pop_compact_events()`.

**Impacto:**

- Evita corrupcion del contexto conversacional.
- Mejora la calidad de respuestas cuando la conversacion excede el contexto.
- Reduce deuda conceptual en middleware avanzado.

**Riesgo si no se atiende:**

- El agente puede perder informacion importante o enviar instrucciones internas como si fueran memoria resumida.
- La compaccion puede degradar respuestas sin fallar explicitamente, lo que la vuelve dificil de diagnosticar.

---

### 3. Refactorizar `AgentEngine.stream` en unidades privadas mas pequenas

**Estado:** pendiente amplio; se aplico solo una extraccion pequena segura.

**Archivos involucrados:**

- `phoson_agent/agent.py`
- `tests/phoson_agent/test_agent_unit.py`
- `tests/phoson_agent/test_agent_engine_integration.py`

**Problema:**

`AgentEngine.stream` concentra demasiadas responsabilidades: control del loop ReAct, aplicacion de middleware, consumo de eventos LLM, acumulacion de usage, ejecucion de tools, manejo de errores, construccion de steps, historial, cancelacion y resultado final.

El metodo es largo y dificil de modificar sin riesgo de regresiones.

**Tarea:**

- Hecho parcial: se extrajo `_get_tool_definitions`.
- Pendiente: extraer el consumo de eventos LLM a un helper tipo `_collect_llm_events(...)` o `_run_llm_step(...)`.
- Pendiente: reemplazar la construccion inline de `RunStep` LLM por `_build_llm_step`.
- Pendiente: reemplazar la construccion inline del assistant message por `_build_assistant_message`.
- Pendiente: reemplazar la ejecucion inline de tools por `_execute_tool_call`, `_build_tool_step` y `_append_tool_result`.
- Pendiente: reemplazar la construccion inline de `AgentRunResult` por `_build_final_result`.
- Pendiente: preservar exactamente el orden de eventos actual.
- Pendiente: agregar o ajustar pruebas para garantizar que el refactor no cambie comportamiento observable.

**Impacto:**

- Mejora mantenibilidad y legibilidad.
- Facilita introducir nuevas features como parallel tool calls, tool timeouts o mejores traces.
- Reduce riesgo de bugs cuando se modifique el loop principal.

**Riesgo si no se atiende:**

- Cada cambio futuro en el agente tendra alto riesgo de romper otra responsabilidad.
- La curva de entrada para nuevos contribuidores sera mayor.

---

### 4. Hacer pasar Ruff en los paquetes objetivo

**Estado:** resuelto para el scope revisado.

**Archivos involucrados:**

- `phoson_agent/__init__.py`
- `phoson_agent/agent.py`
- `phoson_agent/plugin_loader.py`
- `tests/phoson_agent/test_plugin_system.py`
- Posiblemente otros archivos si se expande el scope

**Problema:**

`python -m ruff check phoson_llm phoson_agent tests/phoson_llm tests/phoson_agent` falla con errores de imports desordenados, imports no usados y una variable asignada sin uso.

Errores observados:

- Imports sin ordenar en `phoson_agent/__init__.py`.
- Imports sin ordenar en `phoson_agent/agent.py`.
- Import no usado `PluginSpec` en `phoson_agent/agent.py`.
- Imports sin ordenar en `phoson_agent/plugin_loader.py`.
- Imports sin ordenar y no usados en `tests/phoson_agent/test_plugin_system.py`.
- Variable `engine` asignada sin uso en `tests/phoson_agent/test_plugin_system.py`.

**Tarea:**

- Hecho: `python -m ruff check phoson_llm phoson_agent phoson_cli phoson_plugin_mcp tests/phoson_llm tests/phoson_agent tests/phoson_cli tests/phoson_plugin_mcp` pasa.
- Pendiente opcional: considerar agregar `ruff check` al flujo de CI si aun no existe.

**Impacto:**

- Mejora higiene del repo.
- Evita que deuda pequena se acumule.
- Hace que la configuracion declarada en `pyproject.toml` sea realmente confiable.

**Riesgo si no se atiende:**

- Los checks locales o de CI fallaran cuando se activen.
- Se normaliza aceptar fallas de calidad automatizables.

---

### 5. Alinear documentacion con la API real

**Estado:** parcialmente resuelto; falta barrido completo y snippets testeables.

**Archivos involucrados:**

- `docs/api/phoson_llm.md`
- `docs/api/phoson_agent.md`
- `phoson_llm/__init__.py`
- `phoson_llm/pricing.py`
- `phoson_agent/context.py`
- `phoson_agent/middleware.py`
- `phoson_agent/sessions/storage_jsonl.py`

**Problema:**

La documentacion contiene ejemplos y promesas que no coinciden completamente con el codigo real.

Casos detectados:

- `phoson_llm` documenta `build_chat`, pero no aparece implementado ni exportado.
- `docs/api/phoson_llm.md` muestra un uso de `PriceEntry` con campos que no existen en la clase real.
- `docs/api/phoson_agent.md` muestra `AgentContext(session_id=..., tools=...)`, pero `AgentContext` actualmente solo tiene `extra`.
- `docs/api/phoson_agent.md` documenta parametros de `RetryMiddleware` que no coinciden con la implementacion actual.
- `docs/api/phoson_agent.md` muestra `JsonlStorage(session_dir=...)`, pero la clase real recibe `base_path`.

**Tarea:**

- Hecho: se removio `build_chat` de la documentacion publica de `phoson_llm`.
- Hecho: se corrigio el ejemplo de `PriceEntry`.
- Hecho: se corrigieron ejemplos de `AgentContext`, `RetryMiddleware` y `JsonlStorage`.
- Pendiente: hacer un barrido completo de docs fuera de `docs/api/phoson_llm.md` y `docs/api/phoson_agent.md` para detectar referencias viejas.
- Pendiente: agregar snippets minimos testeables si el proyecto adopta doctests o pruebas de docs.
- Pendiente: documentar explicitamente el contrato actual de `ToolHandler` manual: `(args, context)`.

**Impacto:**

- Reduce friccion para usuarios e integradores.
- Evita errores de onboarding.
- Hace que la API publica sea mas creible.

**Riesgo si no se atiende:**

- Los usuarios copiaran ejemplos que no funcionan.
- Se multiplicaran issues por discrepancias entre docs y runtime.

---

## Prioridad media

### 6. Extraer helpers compartidos entre `OpenAIChat` y `OpenRouterChat`

**Estado:** parcialmente resuelto; falta completar la extraccion y limpiar estilo.

**Archivos involucrados:**

- `phoson_llm/chats/openai.py`
- `phoson_llm/chats/openrouter.py`
- Posible nuevo modulo: `phoson_llm/chats/_openai_compatible.py`

**Problema:**

`OpenAIChat` y `OpenRouterChat` duplican mucha logica: conversion de mensajes, conversion de tools, acumulacion de tool call deltas, parseo de argumentos, manejo de reasoning y usage streaming.

Esto aumenta el costo de mantenimiento y el riesgo de divergencia sutil entre providers compatibles.

**Tarea:**

- Hecho: se creo `phoson_llm/chats/_openai_compatible.py`.
- Hecho: `OpenAIChat` y `OpenRouterChat` reutilizan conversion de mensajes/tools.
- Hecho: `OpenRouterChat` reutiliza `_parse_tool_args` y `_extract_reasoning_delta`.
- Pendiente: extraer una utilidad comun para acumular/finalizar tool calls; aun hay logica duplicada dentro de los adapters.
- Hecho: se reemplazo `__import__("base64")` por import explicito de `base64` en `_openai_compatible.py`.
- Pendiente: parametrizar tipos de retorno (`list[dict]`, `dict`) donde sea razonable.
- Pendiente: cubrir el helper compartido con tests unitarios directos, no solo via imports reexportados desde adapters.

**Impacto:**

- Menos duplicacion.
- Cambios futuros a tool calls o multimodalidad se hacen en un solo lugar.
- Facilita agregar otros providers OpenAI-compatible.

**Riesgo si no se atiende:**

- Los adapters se iran desincronizando.
- Bugs corregidos en un provider persistiran en otro.

---

### 7. Mover demos/scripts fuera de los paquetes runtime

**Estado:** pendiente.

**Archivos involucrados:**

- `phoson_llm/main.py`
- `phoson_agent/main.py`
- `examples/*`
- Posiblemente `README.md`

**Problema:**

`phoson_llm/main.py` y `phoson_agent/main.py` contienen demos, funciones llamadas `test_*`, prints y ejecucion manual. Esto mezcla codigo de libreria con scripts de prueba/demo.

En `phoson_llm/main.py` tambien aparecen API keys placeholder y modelos hardcodeados.

Revision actual: ambos archivos siguen dentro de los paquetes runtime.

**Tarea:**

- Mover demos a `examples/` o `scripts/`.
- Renombrar funciones `test_*` si no son tests de pytest.
- Dejar los paquetes `phoson_llm` y `phoson_agent` solo con runtime/API.
- Actualizar README o docs para apuntar a los ejemplos nuevos.

**Impacto:**

- Paquetes mas limpios y profesionales.
- Menos confusion entre pruebas reales, demos y runtime.
- Reduce riesgo de ejecutar accidentalmente codigo demo desde imports o entrypoints futuros.

**Riesgo si no se atiende:**

- La estructura del paquete se vuelve menos clara.
- Herramientas de testing o discovery pueden interpretar mal demos como tests.

---

### 8. Endurecer tipos en schemas y adapters LLM

**Estado:** pendiente parcial; algunos tipos mejoraron, pero falta trabajo amplio.

**Archivos involucrados:**

- `phoson_llm/schemas/inputs.py`
- `phoson_llm/schemas/outputs.py`
- `phoson_llm/chats/openai.py`
- `phoson_llm/chats/openrouter.py`
- `phoson_llm/chats/anthropic.py`
- `phoson_llm/chats/ollama.py`
- `phoson_agent/models.py`
- `phoson_agent/tool.py`

**Problema:**

Hay varios usos de `dict`, `list[dict]` y `Any` donde el contrato podria ser mas preciso. Esto es normal al integrar APIs externas, pero en una libreria core reduce ayuda del type checker y dificulta refactors seguros.

Revision actual: `ToolHandler` fue endurecido, pero `phoson_llm` todavia mantiene `dict`/`list[dict]` genericos, incluyendo el nuevo helper `_openai_compatible.py`.

Ejemplos:

- `ToolUseBlock.args: dict`
- `ToolDefinition.parameters: dict`
- Helpers que retornan `list[dict]`
- Payloads y args sin shape claro

**Tarea:**

- Definir aliases para JSON: `JsonValue`, `JsonObject`, `JsonSchema`.
- Reemplazar `dict` genericos por `dict[str, Any]` o aliases mas precisos.
- Parametrizar retornos de conversion cuando sea razonable.
- Evitar sobre-tipar respuestas SDK muy dinamicas si vuelve el codigo menos legible.

**Impacto:**

- Mejora soporte de IDE/type checker.
- Reduce errores en refactors.
- Hace mas claro que datos son JSON serializables.

**Riesgo si no se atiende:**

- Bugs de shape se detectaran tarde, en runtime.
- Aumenta la dificultad de mantener providers y tool schemas.

---

### 9. Introducir type checking formal

**Estado:** iniciado; falta ejecutar en entorno sincronizado.

**Archivos involucrados:**

- `pyproject.toml`
- Posible configuracion `pyrightconfig.json` o seccion mypy/pyright
- Todo el paquete gradualmente

**Problema:**

El proyecto usa type hints modernos, pero no parece haber un type checker formal configurado como parte del flujo de calidad. Ya hay comentarios como `# pyright: ignore[...]` y `# type: ignore`, lo que sugiere que eventualmente se penso en esto.

**Tarea:**

- Hecho: se agrego configuracion `[tool.pyright]` en `pyproject.toml` con scope `phoson_llm` y `phoson_agent`.
- Hecho: se agrego `pyright` a dependencias de desarrollo.
- Pendiente critico: ejecutar Pyright y registrar errores reales.
- Pendiente: eliminar ignores innecesarios despues de ver resultados reales.
- Pendiente: documentar el comando de verificacion.

**Impacto:**

- Detecta inconsistencias como la de `ToolHandler` antes de runtime.
- Mejora confianza para refactors.
- Hace la libreria mas robusta para usuarios externos.

**Riesgo si no se atiende:**

- Los type hints quedan como documentacion informal, no como garantia verificable.
- Bugs de contrato pueden sobrevivir aunque los tests pasen.

---

### 10. Mejorar manejo de errores silenciosos

**Archivos involucrados:**

- `phoson_agent/plugins/context_window.py`
- `phoson_agent/agent.py`
- Posiblemente plugin loader y middlewares

**Problema:**

Hay varios `except Exception: pass` o equivalentes. Algunos son razonables como fallback defensivo, pero hoy no dejan trazabilidad.

Ejemplos:

- Fallback silencioso al resolver contexto de Ollama.
- Fallback silencioso al resolver contexto de OpenRouter.
- Cleanup de plugins ignora errores completamente.

**Tarea:**

- Decidir politica de logging para libreria.
- Agregar `logging.getLogger(__name__)` donde haya fallbacks silenciosos.
- Usar `logger.debug(...)` o `logger.warning(...)` segun criticidad.
- Mantener comportamiento tolerante cuando el fallback sea intencional.

**Impacto:**

- Mejora diagnosabilidad sin romper UX.
- Facilita investigar problemas de providers, plugins y resolucion de contexto.

**Riesgo si no se atiende:**

- Fallos externos quedaran invisibles.
- El sistema puede degradar a defaults sin que el usuario sepa por que.

---

### 11. Revisar `stream_sync` y `run_sync` para contextos con event loop existente

**Archivos involucrados:**

- `phoson_llm/chats/base.py`
- `phoson_agent/agent.py`

**Problema:**

`stream_sync` y `run_sync` crean event loops manualmente. Esto funciona en scripts simples, pero puede ser problematico si se llama desde entornos que ya tienen un event loop activo, como notebooks, frameworks async o algunas UIs.

**Tarea:**

- Definir comportamiento esperado si ya existe un loop corriendo.
- Documentar limitaciones actuales.
- Considerar detectar loop activo y emitir error claro.
- Evaluar si vale la pena soportar wrappers sync mas robustos.

**Impacto:**

- Mejora experiencia de integracion.
- Evita errores confusos en notebooks o apps async.

**Riesgo si no se atiende:**

- Usuarios encontraran errores dificiles de entender al usar APIs sync en contextos async.

---

### 12. Revisar I/O sincronico en `JsonlStorage` pese a API async

**Archivos involucrados:**

- `phoson_agent/sessions/storage_jsonl.py`
- `tests/phoson_agent/test_session_storage.py`

**Problema:**

`JsonlStorage` expone metodos async, pero internamente usa operaciones de archivo sincronicas. Para uso pequeno esta bien, pero puede bloquear el event loop si se guardan sesiones grandes o muchas sesiones.

**Tarea:**

- Decidir si `JsonlStorage` es almacenamiento local simple y bloquear es aceptable.
- Si se mantiene sync, documentarlo claramente.
- Si se quiere comportamiento async real, usar `asyncio.to_thread` o una libreria async de archivos.
- Agregar pruebas o benchmarks simples si se espera uso intensivo.

**Impacto:**

- Mejora escalabilidad en apps interactivas.
- Aclara expectativas del storage default.

**Riesgo si no se atiende:**

- El event loop puede bloquearse durante guardado/carga de sesiones grandes.

---

### 13. Hacer mas seguro el loader de plugins por path

**Archivos involucrados:**

- `phoson_agent/plugin_loader.py`
- `tests/phoson_agent/test_plugin_system.py`

**Problema:**

El loader por path modifica globalmente `sys.path` con `insert` y luego `remove`. Esto es practico, pero puede ser fragil con concurrencia, imports anidados o paths repetidos.

**Tarea:**

- Evitar modificar `sys.path` si `spec_from_file_location` basta para cargar el modulo.
- Si se necesita `sys.path`, usar un context manager que restaure el estado exacto.
- Agregar pruebas para paths duplicados o carga concurrente si aplica.

**Impacto:**

- Reduce efectos secundarios globales.
- Mejora robustez del sistema de plugins.

**Riesgo si no se atiende:**

- Imports externos pueden resolverse de forma inesperada.
- Cargas de plugin concurrentes podrian interferirse.

---

## Prioridad baja

### 14. Mejorar consistencia de idioma y estilo en comentarios/docstrings

**Archivos involucrados:**

- `phoson_llm/*`
- `phoson_agent/*`

**Problema:**

Hay mezcla de ingles y espanol en comentarios/docstrings. No rompe nada, pero resta consistencia a una libreria que parece orientada a uso publico.

**Tarea:**

- Elegir idioma principal para docstrings y comentarios tecnicos.
- Reescribir comentarios visibles de API publica en ese idioma.
- Mantener mensajes de usuario/localizacion separados si aplica.

**Impacto:**

- Mejora profesionalismo y consistencia.
- Facilita contribuciones externas.

**Riesgo si no se atiende:**

- Bajo riesgo tecnico, pero aumenta ruido de lectura.

---

### 15. Revisar nombres y semantica de modelos/eventos poco usados

**Archivos involucrados:**

- `phoson_llm/schemas/outputs.py`
- `phoson_agent/models.py`
- Tests asociados

**Problema:**

Algunos modelos/eventos parecen preparados para crecimiento pero no integrados del todo, por ejemplo `LLMModalitiesEvent` y `AgentSubagentResult`. Esto puede estar bien como API futura, pero conviene confirmar si son parte del contrato publico.

**Tarea:**

- Revisar eventos exportados vs eventos realmente emitidos.
- Documentar claramente eventos que existen pero aun no son emitidos.
- Remover o marcar como experimental si no se quieren mantener como contrato publico.

**Impacto:**

- Evita sobreprometer API.
- Reduce superficie publica innecesaria.

**Riesgo si no se atiende:**

- Usuarios pueden depender de eventos que nunca se emiten o cuya semantica aun no esta estable.

---

### 16. Agregar pruebas para documentacion o snippets criticos

**Archivos involucrados:**

- `docs/api/*.md`
- `tests/*`

**Problema:**

Varias discrepancias entre docs y codigo habrian sido detectadas si los snippets principales se verificaran automaticamente.

**Tarea:**

- Identificar snippets criticos de docs.
- Convertirlos a tests unitarios simples o doctests.
- Como minimo, crear tests para factory `build_chat` si se implementa, `@tool`, `AgentEngine`, `JsonlStorage` y pricing.

**Impacto:**

- Mantiene docs sincronizadas.
- Reduce regresiones de API publica.

**Riesgo si no se atiende:**

- La documentacion puede volver a desalinearse conforme cambie el codigo.

---

## Verificaciones actuales

### Tests

Comando ejecutado:

```bash
python -m pytest tests/phoson_llm tests/phoson_agent
```

Resultado:

- `145 passed`
- `1 warning`

Warning observado:

- `tests/phoson_agent/test_plugin_system.py`: pytest no colecta `TestPlugin` como clase de tests porque tiene `__init__`. No parece romper la suite, pero conviene renombrarla si solo es fixture/helper.

### Lint

Comando ejecutado:

```bash
python -m ruff check phoson_llm phoson_agent tests/phoson_llm tests/phoson_agent
```

Resultado:

- Pasa sin errores para el scope revisado.

### Type checking

Comando intentado:

```bash
python -m pyright phoson_llm phoson_agent
```

Resultado:

- No se pudo ejecutar: `No module named pyright`.
- Falta instalar `pyright` o agregarlo a dependencias de desarrollo.

## Meta recomendada

Despues de atender las tareas de prioridad alta, el score tecnico esperado podria subir de aproximadamente `7.1/10` a cerca de `8.0/10` sin una re-arquitectura grande.

El orden recomendado de ejecucion es:

1. Corregir el refactor incompleto de `AgentEngine`: eliminar `_build_llm_step` duplicado y usar/remover helpers nuevos.
2. Agregar tests nuevos para `SummarizationMiddleware.wrap_llm_call`.
3. Completar documentacion del contrato actual de `ToolHandler`.
4. Agregar `pyright` a dev dependencies y ejecutar type checking real.
5. Completar helper compartido OpenAI-compatible: import explicito de `base64`, tipos mas precisos y acumulacion comun de tool calls.
6. Mover demos/scripts fuera de paquetes runtime.
7. Continuar endurecimiento de tipos y pruebas de snippets/docs.
