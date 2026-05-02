# Plugin de Summarization/Compaction de Conversación

## Contexto

Cuando la conversación se acerca al límite de la ventana de contexto del modelo, necesitamos compactar mensajes antiguos mediante summarization para evitar errores de contexto excedido y mantener la conversación fluida.

---

## 1. Context Windows por Provider/Modelo

| Provider | Model | Context Window | Método de consulta |
|----------|-------|---------------|-------------------|
| **Anthropic** | claude-* (todos) | 200,000 tokens | Registry estático (todos igual) |
| **OpenAI** | gpt-4o, gpt-4o-mini | 128,000 tokens | Registry estático |
| **OpenAI** | o3, o4-mini | 128,000 tokens* | Registry estático |
| **Ollama** | cualquier modelo | Variable (por modelo) | `POST /api/show` → `parameters.num_ctx` |
| **OpenRouter** | cualquier modelo | Variable | `GET /api/v1/models` → `context_length` |

*\*o3/o4-mini no tienen documentación oficial confirmada, pero se asume 128k.*

### Estrategia de resolución de context_window:

```
1. Si provider es Ollama → GET http://localhost:11434/api/show {"name": model} → parse num_ctx
2. Si provider es OpenRouter → GET https://openrouter.ai/api/v1/models → buscar model.id → context_length
3. Si provider es Anthropic/OpenAI directo → lookup en registry estático
4. Fallback → 128,000 tokens (conservador)
```

---

## 2. Arquitectura Propuesta

### 2.1. Nuevo módulo: `phoson_agent/plugins/summarizer.py`

Un middleware `SummarizationMiddleware` que se integra al `AgentEngine` existente.

```
phoson_agent/
├── plugins/                          # NUEVO
│   ├── __init__.py
│   ├── context_window.py             # Registry + resolución de context_window
│   └── summarizer.py                 # SummarizationMiddleware
```

### 2.2. Componentes

#### A) `ContextWindowResolver`
```python
class ContextWindowResolver:
    """Resuelve el context_window de un modelo dado su provider y nombre."""
    
    # Registry estático para providers conocidos
    STATIC_REGISTRY = {
        "anthropic/claude-opus-4-7": 200_000,
        "anthropic/claude-opus-4-6": 200_000,
        "anthropic/claude-sonnet-4-6": 200_000,
        "anthropic/claude-haiku-4-5": 200_000,
        "openai/gpt-4o": 128_000,
        "openai/gpt-4o-mini": 128_000,
        "openai/o3": 128_000,
        "openai/o4-mini": 128_000,
    }
    
    async def resolve(self, provider: str, model: str, base_url: str | None = None) -> int:
        """Resuelve context_window. Cachéa resultados de Ollama/OpenRouter."""
```

#### B) `TokenEstimator`
```python
class TokenEstimator:
    """Estima tokens en una lista de mensajes (sin llamar al LLM)."""
    
    @staticmethod
    def estimate_messages(messages: list[Message]) -> int:
        """Estimación rápida: ~4 chars por token para texto, + overhead por tool calls."""
```

#### C) `SummarizationMiddleware(AgentMiddleware)`
```python
@dataclass
class SummarizationMiddleware(AgentMiddleware):
    """
    Middleware que compacta la conversación cuando supera el threshold.
    
    Estrategia:
    - Mantiene siempre el system prompt intacto
    - Mantiene los últimos N mensajes sin tocar
    - Reemplaza mensajes intermedios con un resumen generado por el mismo LLM
    
    Flujo en on_before_llm():
    1. Estimar tokens actuales
    2. Resolver context_window del modelo
    3. Si tokens > threshold (80%):
       a. Separar: system + recientes + intermedios
       b. Llamar al LLM para resumir intermedios
       c. Reemplazar intermedios con "Summary: <resumen>"
    4. Retornar mensajes compactados
    """
    
    threshold: float = 0.80        # 80% del context window
    min_keep_messages: int = 4     # Siempre mantener los últimos 4 mensajes
    summary_prompt: str = "..."    # Prompt para generar el resumen
```

---

## 3. Estrategia de Compaction

### Diagrama de flujo:

```
┌─────────────────────────────────────────┐
│  on_before_llm(messages, config)        │
├─────────────────────────────────────────┤
│                                         │
│  1. context_window = resolver(model)    │
│  2. current_tokens = estimate(messages) │
│  3. threshold = context_window * 0.80   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ current_tokens > threshold?     │   │
│  └──────────┬──────────────────────┘   │
│             │                           │
│       NO    │    SÍ                     │
│      ┌──────┴──────┐                    │
│      ▼             ▼                    │
│  Return        Separar mensajes:       │
│  messages      ├─ system (siempre)     │
│                ├─ recientes (últimos 4)│
│                └─ intermedios (resto)  │
│                       │                 │
│                       ▼                 │
│                LLM Summary Call:        │
│                "Resume esta conversa..."│
│                       │                 │
│                       ▼                 │
│                Reemplazar intermedios   │
│                con [Summary: <text>]    │
│                       │                 │
│                       ▼                 │
│                Return compacted msgs    │
└─────────────────────────────────────────┘
```

### Mensajes resultantes tras compactación:

```
[
  Message(role="system", content="..."),           # System prompt intacto
  Message(role="user", content="[Conversation summary: ...]"),  # Resumen
  Message(role="user", content="..."),              # Últimos mensajes
  Message(role="assistant", content="..."),         # conservados
  ...
]
```

---

## 4. Prompt de Summarization

```
You are summarizing a conversation to reduce its token count while preserving 
all critical information.

Instructions:
1. Summarize the conversation history below, keeping:
   - The user's original goal/task
   - Key decisions made
   - Important context and constraints
   - Results of tool executions (especially file contents, code, data)
   - Current progress and what remains to be done
2. Be concise but thorough.
3. Preserve any code snippets, file paths, or technical details that are relevant.
4. Output ONLY the summary, no preamble.

Conversation history to summarize:
{messages_text}
```

---

## 5. Integración con AgentEngine

### Uso:

```python
from phoson_agent.plugins.summarizer import SummarizationMiddleware
from phoson_agent.agent import AgentEngine
from phoson_llm.chats.anthropic import AnthropicChat

chat = AnthropicChat()
summarizer = SummarizationMiddleware(
    threshold=0.80,           # 80% del context window
    min_keep_messages=4,      # Mantener últimos 4 mensajes
)

engine = AgentEngine(
    chat=chat,
    tools=my_tools,
    middlewares=[summarizer],  # Se añade al pipeline de middlewares
)
```

### ¿Por qué middleware y no plugin separado?

- Ya existe el sistema de middleware con hooks `on_before_llm`
- `on_before_llm` es el punto perfecto: se ejecuta antes de cada llamada al LLM
- El middleware puede inspeccionar y modificar los mensajes
- No requiere cambios en `AgentEngine`

---

## 6. Consideraciones

### Estimación de tokens
- **Opción A (simple)**: `len(text) // 4` — rápido, ~90% accuracy
- **Opción B (precisa)**: Usar `tiktoken` para OpenAI, estimación para Anthropic
- **Recomendación**: Opción A para el check rápido, con margen de seguridad (80% vs 90%)

### Summarization call
- El resumen se genera con el **mismo modelo** que la conversación
- Se hace una llamada LLM adicional (costo extra)
- Se puede optimizar: si el costo de sumarizar > beneficio, no compactar

### Ollama
- `POST /api/show` con `{"name": "llama3.2"}` retorna `parameters.num_ctx`
- Cachear resultados para no consultar cada vez

### OpenRouter
- `GET /api/v1/models` retorna lista con `context_length`
- Cachear resultados (la lista no cambia frecuentemente)
- Requiere API key para algunos campos

### Edge cases
- Si la conversación ya tiene un resumen previo, append al resumen en lugar de re-summarizar todo
- Nunca compactar si hay menos de `min_keep_messages + 2` mensajes
- System prompt nunca se toca

---

## 7. Archivos a crear

| Archivo | Descripción |
|---------|-------------|
| `phoson_agent/plugins/__init__.py` | Init del paquete plugins |
| `phoson_agent/plugins/context_window.py` | Registry + resolver de context windows |
| `phoson_agent/plugins/summarizer.py` | SummarizationMiddleware + TokenEstimator |
| `tests/phoson_agent/test_summarizer.py` | Tests unitarios del middleware |

---

## 8. Eventos adicionales (opcional)

Se podrían agregar eventos para informar al UI cuando ocurre compactación:

```python
@dataclass
class SummarizationEvent(AgentEvent):
    """Emitido cuando se compacta la conversación."""
    original_tokens: int = 0
    compacted_tokens: int = 0
    messages_removed: int = 0
    summary_length: int = 0
```

---

¿Qué te parece la propuesta? ¿Quieres que proceda con la implementación o hay algo que ajustar?
