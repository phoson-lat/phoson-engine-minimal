# phoson-engine-minimal

> Motor de agentes de Phoson — construido desde cero, sin frameworks de abstracción.

---

## Contexto del proyecto

**Phoson** es una empresa de infraestructura tecnológica. Su primer producto es una plataforma de agentes autónomos. Este repositorio es el núcleo de ese producto: el engine que hace correr los agentes.

La decisión de arquitectura más importante ya está tomada: **no usar LangChain, LangGraph ni ningún framework de agentes**. El engine se construye sobre SDKs nativos de los providers + un ReAct loop custom (~150 líneas). La razón es tener control total sobre observabilidad (RunSteps), streaming normalizado y MCP.

La inspiración de arquitectura es **Pi** (mariozechner.at) — el agente más minimal que existe: system prompt corto, 4 tools, loop simple. Phoson va más allá: multi-tenant, observable, con memoria y sandbox de ejecución.

---

## Estructura del repositorio

```
phoson-engine-minimal/
├── phoson_llm/              # Capa 1: normalizador de LLMs (ACTIVO)
│   ├── schemas/
│   │   ├── __init__.py      # reexporta todo
│   │   ├── inputs.py        # Message, ContentBlocks, ToolDefinition, ModelConfig
│   │   └── outputs.py       # LLMEvent y subclases, TokenUsage, UsageEvent
│   ├── chats/
│   │   ├── base.py          # BaseLLMChat (ABC) — stream/complete/sync
│   │   ├── anthropic.py     # Adapter Anthropic (thinking, tools, cache)
│   │   └── openai.py        # Adapter OpenAI + Ollama + OpenRouter
│   ├── pricing.py           # Tabla de precios por modelo, calculate_cost()
│   └── main.py              # Script de pruebas
│
├── phoson_agent/            # Capa 2: agent loop (EN PROGRESO)
│   ├── __init__.py          # API pública del agent
│   ├── agent.py             # ReAct loop stateless + wrappers sync/async
│   └── models.py            # AgentTool, RunStep, AgentRunResult
│
├── pyproject.toml
└── uv.lock
```

---

## Capa 1: `phoson_llm` — estado actual ✅

`phoson_llm` es el normalizador de LLMs. Abstrae las diferencias entre providers y devuelve siempre el mismo stream de eventos tipados.

### Interfaz pública

```python
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.schemas import Message, ModelConfig, ToolDefinition

chat = AnthropicChat()  # o OpenAIChat()

async for event in chat.stream(messages, config, tools):
    match event:
        case TokenEvent():       ...  # fragmento de texto
        case ToolCallEvent():    ...  # tool call lista para ejecutar
        case UsageEvent():       ...  # tokens + costo USD
        case LLMDoneEvent():     ...  # respuesta completa
```

Cuatro métodos disponibles en todo adapter:
- `stream()` — async generator, evento por evento
- `complete()` — async, devuelve solo `LLMDoneEvent`
- `stream_sync()` — sync generator (para CLI/workers)
- `complete_sync()` — sync, devuelve solo `LLMDoneEvent`

### Eventos (`phoson_llm/schemas/outputs.py`)

| Evento | Descripción |
|---|---|
| `LLMStartEvent` | Inicio del call. Lleva `model` y `message_count` |
| `TokenEvent` | Fragmento de texto. Llega token a token |
| `ReasoningStartEvent` | El modelo empezó a razonar (Anthropic thinking / OpenAI o1) |
| `ReasoningTokenEvent` | Fragmento del bloque de reasoning |
| `ReasoningDoneEvent` | Reasoning completo ensamblado |
| `ToolCallDeltaEvent` | Chunk parcial de args de una tool (para UI en tiempo real) |
| `ToolCallEvent` | Tool call completa con args parseados. **El agent loop actúa sobre este** |
| `UsageEvent` | Tokens + `cost_usd` (costo real al provider). Uno por LLM call |
| `LLMDoneEvent` | Texto completo ensamblado. Siempre el último evento |
| `ErrorEvent` | Error con `code`, `message`, `retryable` |

### Inputs (`phoson_llm/schemas/inputs.py`)

```python
@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str | list[TextBlock | ToolUseBlock | ToolResultBlock]

@dataclass
class ModelConfig:
    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    system: str | None = None
    thinking_budget: int | None = None          # Anthropic extended thinking
    reasoning_effort: Literal[...] | None = None # OpenAI o1/o3

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict   # JSON Schema
```

### Providers soportados

| Provider | Adapter | Notas |
|---|---|---|
| Anthropic | `AnthropicChat` | Thinking, tool use, prompt caching |
| OpenAI | `OpenAIChat` | Tool use, reasoning_effort para o1/o3 |
| OpenRouter | `OpenAIChat(base_url=..., api_key=...)` | Compatible OpenAI; usage puede ser 0 en modelos free |
| Ollama | `OpenAIChat(base_url="http://localhost:11434/v1", api_key="ollama")` | Tool calling depende del modelo |

### Pricing (`phoson_llm/pricing.py`)

```python
from phoson_llm.pricing import calculate_cost

cost_usd, cost_known = calculate_cost(
    model="claude-sonnet-4-6",
    input_tokens=1000,
    output_tokens=500,
    cache_write_tokens=0,
    cache_read_tokens=200,
)
# cost_known=False para modelos locales (Ollama) sin precio conocido
```

Modelos en tabla: `claude-opus-4-6/4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`, `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini`, `gemini-2.5-pro/flash/flash-lite`, `gemini-2.0-flash`.

---

## Capa 2: `phoson_agent` — estado actual (Fase 2 en curso)

`phoson_agent` monta un loop ReAct minimal encima de `phoson_llm`.

Objetivos de esta fase:

- ReAct loop: `stream()` → detectar `ToolCallEvent` → ejecutar tool → retroalimentar → repetir
- Gestión del historial de mensajes (`list[Message]`) sin estado interno persistente entre runs
- Emitir `RunStep`s básicos para observabilidad (llm/tool, duración, errores)
- Calcular `credits = UsageEvent.cost_usd * phoson_weight`
- Condición de parada: `LLMDoneEvent.has_tool_calls == False`

Decisión de diseño: el estado vive en el historial entregado al run; el engine solo transforma ese historial durante la ejecución y devuelve el resultado completo.

Interfaz objetivo de esta fase:

```python
from phoson_agent import AgentEngine, AgentTool

engine = AgentEngine(chat=OpenAIChat(), tools=[...], phoson_weight=1.2)
result = await engine.run(messages, config)

print(result.final_content)
print(result.total_credits)
for step in result.steps:
    print(step.kind, step.duration_ms)
```

---

## Known issues / bugs pendientes

### Tool call duplicado en OpenRouter ✅ FIXED
Resuelto en `phoson_llm/chats/openai.py` usando un flag local para garantizar emisión única de `ToolCallEvent` cuando `finish_reason == "tool_calls"` llega más de una vez.

### Wrappers `complete()/stream_sync()` en `BaseLLMChat` ✅ FIXED
Resuelto en `phoson_llm/chats/base.py`: ahora consumen correctamente async generators sin `await` inválido sobre `stream()`.

### Usage en 0 con modelos free de OpenRouter
OpenRouter no siempre respeta `stream_options={"include_usage": True}` en modelos gratuitos. `cost_known` será `False` en esos casos y `cost_usd` será `0.0`. Comportamiento esperado.

---

## Stack

```
Python 3.12
uv (package manager)
anthropic SDK
openai SDK
```

## Variables de entorno

```env
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=   # opcional, va en base_url del OpenAIChat
```

## Correr pruebas

```bash
# Activar tests en main.py cambiando los flags a True
PYTHONPATH=$PYTHONPATH:$(pwd) python phoson_llm/main.py
```

---

## Principios de diseño

- **Sin frameworks de abstracción** — SDKs nativos + código propio
- **Un solo abstractmethod** — `BaseLLMChat.stream()`. Los adapters implementan solo eso
- **Eventos tipados, no dicts** — dataclasses con `match/case` en el consumer
- **Separación inputs/outputs** — `schemas/inputs.py` es lo que le das al LLM, `schemas/outputs.py` es lo que te devuelve
- **Costo en la capa LLM** — `phoson_llm` calcula `cost_usd` (precio real del provider). El agent loop aplica `phoson_weight` para obtener créditos del usuario
- **Observable por diseño** — cada evento lleva `timestamp`. `LLMStartEvent` + `LLMDoneEvent` dan `duration_ms` del call sin instrumentación extra
