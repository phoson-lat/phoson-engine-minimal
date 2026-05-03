# phoson_llm

Unified LLM chat interface providing a normalized API across multiple providers.

## Overview

`phoson_llm` provides a consistent interface for interacting with various LLM providers:

- **OpenAI** — GPT-4, GPT-4o, GPT-3.5 Turbo
- **Anthropic** — Claude 3.5, Claude 3, Claude 2 (with extended thinking, tool use, prompt caching)
- **OpenRouter** — Multi-provider aggregation with unified pricing
- **Ollama** — Local LLM inference (Llama, Mistral, etc.)

## Chat Adapters

### BaseLLMChat

Abstract base class for all LLM adapters.

```python
from phoson_llm.chats import BaseLLMChat
```

**Methods:**

- `stream(messages, config, tools)` — Async streaming of LLM events
- `complete(messages, config, tools)` — Async non-streaming completion
- `stream_sync(messages, config, tools)` — Sync streaming
- `complete_sync(messages, config, tools)` — Sync non-streaming

**Event Order in stream():**

1. `LLMStartEvent`
2. `(ReasoningStartEvent → ReasoningTokenEvent* → ReasoningDoneEvent)?`
3. `(TokenEvent | ToolCallDeltaEvent | ToolCallEvent)*`
4. `UsageEvent`
5. `LLMDoneEvent | ErrorEvent`

### OpenAIChat

```python
from phoson_llm.chats import OpenAIChat

chat = OpenAIChat(api_key="sk-...")
```

Supports: streaming, tools, multimodal inputs (images, audio, video, documents).

### AnthropicChat

```python
from phoson_llm.chats import AnthropicChat

chat = AnthropicChat(api_key="sk-ant-...")
```

Supports: streaming, extended thinking, tool use, prompt caching, multimodal inputs.

### OpenRouterChat

```python
from phoson_llm.chats import OpenRouterChat

chat = OpenRouterChat(api_key="sk-or-...")
```

Multi-provider aggregation with unified interface.

### OllamaChat

```python
from phoson_llm.chats import OllamaChat

chat = OllamaChat(base_url="http://localhost:11434")
```

Local LLM inference. Supports: streaming, tools.

## Build Chat Factory

```python
from phoson_llm import build_chat

chat = build_chat("openai", api_key="sk-...")
```

| Provider   | Required Args              | Optional Args                     |
|------------|----------------------------|-----------------------------------|
| `"openai"` | `api_key`                  | `base_url`                        |
| `"anthropic"` | `api_key` (or env var)   | —                                 |
| `"openrouter"` | `api_key`               | `base_url`                        |
| `"ollama"` | —                           | `base_url` (default: localhost)   |

## Schemas

### Message

```python
from phoson_llm import Message

msg = Message(role="user", content="Hello!")
```

| Field     | Type                     | Description                          |
|-----------|--------------------------|--------------------------------------|
| `role`    | `Literal["system", "user", "assistant", "tool"]` | Message sender |
| `content` | `str \| list[ContentBlock]` | Message content                      |
| `name`    | `str \| None`           | For tool/assistant messages          |
| `tool_call_id` | `str \| None`       | For tool messages                    |

### ModelConfig

```python
from phoson_llm import ModelConfig

config = ModelConfig(
    model="gpt-4o",
    max_tokens=4096,
    temperature=0.7,
    system="You are a helpful assistant",
    thinking_budget=None,  # Only for Anthropic/OpenAI o1
)
```

| Field              | Type          | Default | Description                          |
|--------------------|---------------|---------|--------------------------------------|
| `model`            | `str`         | required | Model identifier                     |
| `max_tokens`       | `int`         | 4096    | Max output tokens (max 32,768)       |
| `temperature`      | `float \| None` | None   | Sampling temperature                 |
| `system`           | `str \| None` | None    | System prompt                        |
| `thinking_budget`   | `int \| None` | None    | For extended thinking (Anthropic/OpenAI o1) |

### Content Blocks

- `TextBlock` — Plain text content
- `ImageBlock` — Image input (URL or base64)
- `AudioBlock` — Audio input (URL or base64)
- `VideoBlock` — Video input (URL or base64)
- `DocumentBlock` — Document input (file reference)
- `ToolUseBlock` — Tool call request
- `ToolResultBlock` — Tool execution result

### ToolDefinition

```python
from phoson_llm import ToolDefinition, Message

tool = ToolDefinition(
    name="get_weather",
    description="Get weather for a location",
    parameters={
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"]
    }
)
```

## Event Types

### LLMEvent (base)

All events inherit from `LLMEvent`:

```python
@dataclass
class LLMEvent:
    timestamp: datetime.datetime
```

### Lifecycle Events

- `LLMStartEvent` — LLM call started (`model`, `message_count`)
- `LLMDoneEvent` — LLM call completed (`content`, `has_tool_calls`)

### Text Events

- `TokenEvent` — Text token received (`content`)

### Reasoning Events (Anthropic/OpenAI o1)

- `ReasoningStartEvent` — Reasoning started
- `ReasoningTokenEvent` — Reasoning token (`content`)
- `ReasoningDoneEvent` — Reasoning completed (`content`)

### Tool Call Events

- `ToolCallDeltaEvent` — Partial tool call args (`index`, `tool_name`, `args_chunk`)
- `ToolCallEvent` — Complete tool call (`index`, `tool_call_id`, `tool_name`, `args`)

### Usage Events

```python
@dataclass
class TokenUsage:
    input: int       # Input tokens
    output: int       # Output tokens
    cache_write: int   # Cache write tokens (Anthropic)
    cache_read: int   # Cache read tokens (Anthropic)

@dataclass
class UsageEvent(LLMEvent):
    model: str
    usage: TokenUsage
    cost_usd: float
    cost_known: bool
```

### Error Events

```python
@dataclass
class ErrorEvent(LLMEvent):
    message: str
    code: str | None
    retryable: bool
```

## Pricing

```python
from phoson_llm import calculate_cost, PriceEntry

cost = calculate_cost(
    model="gpt-4o",
    input_tokens=1000,
    output_tokens=500,
)
# Returns: 0.015 (USD)
```

Use `PriceEntry` to get detailed pricing information:

```python
from phoson_llm import PriceEntry

entry = PriceEntry(model="gpt-4o")
print(entry.cost_per_million_input_tokens)  # 2.50
print(entry.cost_per_million_output_tokens)  # 10.00
```

## Public API

```python
from phoson_llm import (
    # Chat adapters
    BaseLLMChat, OpenAIChat, AnthropicChat, OllamaChat, OpenRouterChat,
    # Schemas
    Message, TextBlock, ImageBlock, AudioBlock, VideoBlock, DocumentBlock,
    ToolDefinition, ToolUseBlock, ToolResultBlock, ModelConfig, ContentBlock,
    # Events
    LLMEvent, LLMStartEvent, LLMDoneEvent, TokenEvent,
    ReasoningStartEvent, ReasoningTokenEvent, ReasoningDoneEvent,
    ToolCallEvent, ToolCallDeltaEvent, UsageEvent, TokenUsage, ErrorEvent,
    # Pricing
    calculate_cost, PriceEntry,
    # Factory
    build_chat,
)
```