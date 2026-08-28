# IMPROVEMENTS — phoson-engine-minimal / phoson-cli

> **Origen:** Roadmap activo de resolución de issues abiertos de GitHub para `phoson-engine-minimal` y `phoson-cli`.
>
> **Cómo usar este documento:** Cada ítem corresponde a un issue abierto en GitHub con su prioridad (P0–P2), estimación de esfuerzo (S/M/L), análisis de causa raíz, solución propuesta y criterios de aceptación.
>
> **Estado de referencia:** v0.13.4 · 1408 tests passing · pyright 0 errors · ruff clean.

---

## Tabla resumen de Issues abiertos

| ID | Issue | Título | Prioridad | Esfuerzo | Impacto | Estado |
|----|-------|--------|-----------|----------|---------|--------|
| **I-91** | [#91](https://github.com/phoson-lat/phoson-engine-minimal/issues/91) | Context auto-compact gate subestima tokens & sin fallback en provider 400 | **P0** | M | 🔴 Crítico (bloquea sesiones largas) | ✅ Resuelto (v0.13.5) |
| **I-88** | [#88](https://github.com/phoson-lat/phoson-engine-minimal/issues/88) | Costo/uso en cabecera no se actualiza en vivo + costo OpenRouter USD en $0 | **P0** | S | 🔴 Alto (visibilidad de costos) | ✅ Resuelto (v0.13.6) |
| **I-89** | [#89](https://github.com/phoson-lat/phoson-engine-minimal/issues/89) | `/model` no persiste el provider junto con el modelo en `config.toml` | **P1** | S | 🟠 Medio (inconsistencia de config) | ✅ Resuelto (v0.13.7) |
| **I-82** | [#82](https://github.com/phoson-lat/phoson-engine-minimal/issues/82) | vLLM provider: HTTP 400 "No user query found in messages" con Qwen3.x | **P1** | S-M | 🟠 Medio (soporte local vLLM/Qwen) | ⬜ Abierto |
| **I-83** | [#83](https://github.com/phoson-lat/phoson-engine-minimal/issues/83) | Compactar paneles de error a 1 línea y sobreescribir en cada reintento | **P1** | S-M | 🟠 Medio (ruido visual en TUI) | ⬜ Abierto |
| **I-84** | [#84](https://github.com/phoson-lat/phoson-engine-minimal/issues/84) | Reducción de uso de CPU en la TUI full-screen (idle y streaming) | **P1** | M | 🟠 Medio (eficiencia y batería) | ⬜ Abierto |
| **I-100** | [#100](https://github.com/phoson-lat/phoson-engine-minimal/issues/100) | Activar/desactivar MCPs a nivel servidor y nivel herramienta | **P2** | M-L | 🟡 Medio (gestión granular de tools) | ⬜ Abierto |
| **I-93** | [#93](https://github.com/phoson-lat/phoson-engine-minimal/issues/93) | Paquetes preconstruidos para Linux, macOS y Windows | **P2** | L | 🟢 Bajo (distribución binaria standalone) | ⬜ Abierto |

---

## Detalle de Issues y Plan de Acción

### I-91 — [Bug #91] Auto-compact subestima tokens & falta de fallback en error 400 de ventana de contexto
* **Estado:** ✅ **Resuelto (v0.13.5)** — gate conservador a nivel de request + rescate de emergencia ante 400 de contexto + compactación persistente (ver CHANGELOG v0.13.5).
* **Área:** `phoson_agent/plugins/summarizer.py`, `phoson_cli/controller.py`
* **Prioridad:** **P0** · **Esfuerzo:** M · **Impacto:** 🔴 Crítico
* **Problema:** 
  1. El gate de auto-compactación calcula tokens de forma optimista o incompleta (omitiendo overhead de tool definitions, reasoning o attachments), por lo que el auto-compact no dispara a tiempo.
  2. Cuando el proveedor rechaza la petición con HTTP 400 (context window exceeded / prompt too long), no hay un handler de recuperación que intente forzar la compactación de emergencia y reintentar.
* **Solución propuesta:**
  - Ajustar el cálculo/estimación del gate de token count para ser conservador e incluir el peso de schemas de herramientas y reasoning blocks.
  - Implementar interceptor en el controller / retry middleware: ante un error 400 identificable como "context length exceeded", disparar compactación de emergencia automática del historial y reintentar el turno una vez antes de fallar.
* **Criterio de listo:**
  - Test simulando límite de contexto: dispara auto-compact antes del 100%.
  - Test con mock de provider arrojando 400 context error: activa auto-compact de rescate y continúa la sesión.

---

### I-88 — [Bug #88] Actualización en vivo de tokens/costo en Header + captura de costo USD en OpenRouter
* **Estado:** ✅ **Resuelto (v0.13.6)** — costo `usage.cost` de OpenRouter autoritativo + métricas en vivo por step (ver CHANGELOG v0.13.6).
* **Área:** `phoson_cli/fullscreen/app.py`, `phoson_llm/chats/openrouter.py`
* **Prioridad:** **P0** · **Esfuerzo:** S · **Impacto:** 🔴 Alto
* **Problema:**
  1. El Header de la TUI no refresca el uso de tokens y costo en cada paso del run (solo salta al completarse el turno completo).
  2. El adapter de OpenRouter descarta los metadatos de costo en USD devueltos por la API o no los mapea a `TokenUsage` / métricas de sesión.
* **Solución propuesta:**
  - Enviar eventos de actualización al header durante `AgentStepDoneEvent` para reflejar consumo paso a paso.
  - Extraer el campo de costo en la respuesta streaming/final de OpenRouter y alimentar el acumulador de `SessionMetrics`.
* **Criterio de listo:**
  - Header actualiza sus números en vivo tras cada llamada a herramienta/step.
  - OpenRouter reporta costo mayor a `$0.0000` cuando la API entrega el costo del request.

---

### I-89 — [Enhancement #89] `/model` debe persistir el `provider` correspondiente en `config.toml`
* **Estado:** ✅ **Resuelto (v0.13.7)** — `/model` infiere el provider del modelo elegido (picker o prefijo `vendor/`, con excepción para routers) y persiste la dupla `(provider, model)`; rechaza guardar duplas sin credenciales (ver CHANGELOG v0.13.7).
* **Área:** `phoson_cli/commands.py`, `phoson_cli/config.py`, `phoson_cli/model_picker.py`
* **Prioridad:** **P1** · **Esfuerzo:** S · **Impacto:** 🟠 Medio
* **Problema:** Al ejecutar `/model` (o seleccionarlo en el picker), solo se actualiza la clave `model` en `config.toml`, dejando la clave `provider` desalineada si el nuevo modelo pertenecía a otro proveedor.
* **Solución propuesta:**
  - Vincular la selección de modelo con su proveedor asociado (`provider, model`).
  - Al persistir `/model`, invocar `save_config` actualizando tanto `model` como `provider`.
* **Criterio de listo:**
  - Ejecutar `/model <modelo_de_otro_provider>` actualiza ambos campos en `~/.phoson/config.toml`.
  - Reiniciar el CLI mantiene la dupla `(provider, model)` correcta.

---

### I-82 — [Bug #82] vLLM: HTTP 400 "No user query found in messages" con modelos Qwen3.x
* **Área:** `phoson_llm/chats/vllm.py`, `phoson_agent/agent.py`
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio
* **Problema:** El template de chat de Qwen en vLLM requiere una estructura estricta de mensajes entre llamadas a herramientas (o rechaza historiales donde no detecta un turno de usuario intercalado adecuadamente tras tool responses).
* **Solución propuesta:**
  - Normalizar el historial de mensajes antes de enviarlo al adapter de vLLM, asegurando compatibilidad con el chat template de Qwen / OpenAI standard tool-call formatting.
  - Documentar consideraciones para vLLM + Qwen en la guía de proveedores.
* **Criterio de listo:**
  - Secuencia de múltiples tool calls seguidas se ejecuta sin error 400 en vLLM con template de Qwen.

---

### I-83 — [Enhancement #83] Compactar errores del modelo a 1 línea y sobreescribir en cada reintento
* **Área:** `phoson_cli/fullscreen/render.py`, `phoson_cli/formatting.py`
* **Prioridad:** **P1** · **Esfuerzo:** S-M · **Impacto:** 🟠 Medio
* **Problema:** Los errores de API/red renderizan paneles grandes con JSON crudo que se apilan con cada reintento, ensuciando el transcript.
* **Solución propuesta:**
  - Resumir mensajes de error comunes a una sola línea con badge de advertencia y hint accionable.
  - En la TUI, mutar/reemplazar el bloque de error del reintento previo en vez de añadir un nuevo bloque por cada reintento fallido.
* **Criterio de listo:**
  - Tres reintentos fallidos ocupan solo 1 línea en el transcript en lugar de 3 paneles apilados.

---

### I-84 — [Performance #84] Reducción de uso de CPU en TUI full-screen
* **Área:** `phoson_cli/fullscreen/app.py`, `phoson_cli/fullscreen/sink.py`
* **Prioridad:** **P1** · **Esfuerzo:** M · **Impacto:** 🟠 Medio
* **Problema:** Uso continuo de 5–15% CPU en idle y 15–20% en streaming debido a re-renderizados innecesarios o tickers de animación muy agresivos.
* **Solución propuesta:**
  - Pausar los spinners / tickers cuando la aplicación esté en estado idle esperando input.
  - Throttling adaptativo del render durante el streaming de tokens.
* **Criterio de listo:**
  - Uso de CPU en idle cercano al 0% (<1%).
  - Reducción medible del consumo durante streaming sostenido.

---

### I-100 — [Feature #100] Habilitar / Deshabilitar MCPs a nivel servidor y herramienta
* **Área:** `phoson_mcp/`, `phoson_cli/commands.py`
* **Prioridad:** **P2** · **Esfuerzo:** M-L · **Impacto:** 🟡 Medio
* **Problema:** No existe forma de desactivar temporalmente un servidor MCP completo o una herramienta MCP específica sin borrar la configuración.
* **Solución propuesta:**
  - Añadir soporte para flag `enabled: bool` en la configuración de MCP servers y en tools individuales.
  - Comando `/mcp toggle <server> [tool]` y filtrado en el registro de tools expuestas al modelo.
* **Criterio de listo:**
  - Servidor desactivado no expone ninguna de sus herramientas.
  - Herramienta específica desactivada se omite del schema enviado al LLM.

---

### I-93 — [Feature #93] Empaquetado binario preconstruido (Linux / macOS / Windows)
* **Área:** `.github/workflows/`, CI / packaging tooling (PyInstaller / PyOxidizer / Shiv)
* **Prioridad:** **P2** · **Esfuerzo:** L · **Impacto:** 🟢 Bajo
* **Problema:** Requiere entorno Python ≥3.12 y herramientas de gestión (`uv`/`pip`) para la instalación de usuarios finales.
* **Solución propuesta:**
  - Configurar workflow de release con `PyInstaller` o standalone binary packaging en GitHub Actions para plataformas x86_64 y ARM64.
* **Criterio de listo:**
  - Binarios autónomos descargables desde la sección de Releases de GitHub.

---

## Roadmap sugerido de ataque

```
Sprint Próximo (Estabilidad de Contexto & Métricas)
├── I-91 (Auto-compact gate + fallback 400) ✅ v0.13.5
├── I-88 (Header live metrics + OpenRouter USD cost) ✅ v0.13.6
└── I-89 (/model persiste provider en config.toml) ✅ v0.13.7

Sprint Siguiente (UX & Performance)
├── I-83 (Compactar paneles de error a 1 línea en reintentos)
├── I-84 (Optimización de CPU en idle/streaming)
└── I-82 (vLLM compatibilidad Qwen3.x chat template)

Sprint Posterior (Ecosistema & Distribución)
├── I-100 (Toggle granular MCP servers & tools)
└── I-93 (Binarios precompilados standalone en CI)
```

## Principios de desarrollo

1. **Mantener paridad entre frontends:** Cualquier render nuevo debe ser una función pura en `formatting.py` utilizable en modo fullscreen y clásico.
2. **Cobertura de tests rigurosa:** Cada corrección o feature debe incluir tests unitarios/e2e y pasar validación estricta de `ruff` y `pyright`.
3. **Optimización con métricas:** Todo cambio de performance (CPU, tokens, tiempo) debe incluir benchmark o medición verificable.
