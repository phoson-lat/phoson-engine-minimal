# phoson_llm

Unified LLM chat interface providing a normalized API across multiple providers.

## Overview

`phoson_llm` provides a consistent interface for interacting with various LLM providers:

- **OpenAI** — GPT-4, GPT-4o, GPT-3.5 Turbo
- **Anthropic** — Claude 3.5, Claude 3, Claude 2 (with extended thinking, tool use, prompt caching)
- **OpenRouter** — Multi-provider aggregation with unified pricing
- **Ollama** — Local LLM inference (Llama, Mistral, etc.)
- **GitHub Models** — Free access to Llama 3, Mistral, Phi-3, GPT-4o for developers
- **NVIDIA** — GPU-accelerated inference for open models (Llama 3, Nemotron, Mistral)
- **Groq** — Ultra-fast LPU inference (Llama 3, Mixtral)
- **DeepSeek** — High-performance Chinese LLMs (DeepSeek-V3, DeepSeek-Coder)
- **xAI (Grok)** — Grok-1, Grok-2, Grok-2 Vision
- **Google Gemini** — Gemini 1.5 Pro, Flash, 2.0 Flash (Native SDK support)
- **Mistral AI** — Mistral Large, Mistral Small, Codestral (Native SDK support)
- **AWS Bedrock** — Enterprise-grade access to Anthropic, Meta, Mistral models (Native SDK support)
- **Azure OpenAI** — Enterprise OpenAI deployments with custom URL support
- **And many others** — Together AI, Perplexity, Fireworks, Cohere, LM Studio, vLLM

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

Supports: streaming, extended thinking, tool use, prompt caching (automatic, see below), multimodal inputs.

### OpenRouterChat

```python
from phoson_llm.chats import OpenRouterChat

chat = OpenRouterChat(api_key="sk-or-...")
```

Multi-provider aggregation with unified interface. Forwards `ModelConfig.session_id` as the sticky-routing key and enables automatic caching on `anthropic/*` models (see Prompt Caching below). Identifies itself as *phoson-cli* in OpenRouter app rankings by default (`http_referer` / `app_title` override).

### OllamaChat

```python
from phoson_llm.chats import OllamaChat

chat = OllamaChat(base_url="http://localhost:11434")
```

Local LLM inference. Supports: streaming, tools.

### OpenAI-Compatible Adapters

Many providers use the OpenAI API format. Phoson provides dedicated classes for these for better discoverability and environment variable mapping.

| Class | Provider | Default Env Var |
|-------|----------|-----------------|
| `GitHubModelsChat` | GitHub Models | `GITHUB_TOKEN` |
| `NVIDIAChat` | NVIDIA NIM | `NVIDIA_API_KEY` |
| `GrokChat` | xAI Grok | `XAI_API_KEY` |
| `GroqChat` | Groq | `GROQ_API_KEY` |
| `DeepSeekChat` | DeepSeek | `DEEPSEEK_API_KEY` |
| `TogetherChat` | Together AI | `TOGETHER_API_KEY` |
| `PerplexityChat` | Perplexity | `PERPLEXITY_API_KEY` |
| `FireworksChat` | Fireworks AI | `FIREWORKS_API_KEY` |
| `CohereChat` | Cohere | `COHERE_API_KEY` |
| `LMStudioChat` | LM Studio | N/A (local) |
| `VLLMChat` | vLLM | N/A (local) |

Example:
```python
from phoson_llm.chats import GroqChat

chat = GroqChat(api_key="gsk-...")
```

### AzureChat

```python
from phoson_llm.chats import AzureChat

chat = AzureChat(
    api_key="...",
    base_url="https://RESOURCE_NAME.openai.azure.com",
    api_version="2024-05-01-preview"
)
```

### Native SDK Adapters

These adapters use native provider SDKs instead of the OpenAI compatibility layer. They may require optional dependencies.

#### GeminiChat (Google)

Requires `google-genai`. Install with `pip install "phoson-llm[gemini]"`.

```python
from phoson_llm.chats import GeminiChat

chat = GeminiChat(api_key="...")
```

#### MistralChat

Requires `mistralai`. Install with `pip install "phoson-llm[mistral]"`.

```python
from phoson_llm.chats import MistralChat

chat = MistralChat(api_key="...")
```

#### BedrockChat (AWS)

Requires `boto3`. Install with `pip install "phoson-llm[aws]"`.

```python
from phoson_llm.chats import BedrockChat

chat = BedrockChat(
    region_name="us-east-1",
    aws_access_key_id="...",
    aws_secret_access_key="..."
)
```

## Prompt Caching

Prompt caching lets a provider bill the stable prefix of a request (system
prompt, tools, prior conversation) at a discounted cache-read rate instead
of the full input price. Phoson builds requests so the prefix stays
cacheable and surfaces the cached-token usage on every `UsageEvent`.

### Stable prefix

The CLI system prompt is a stable prefix by design: it carries the **date**
(not a live clock), the working directory, the platform and the tool list —
all constant for the session. Anything that changed per request would bust
the provider's cache for the whole prefix, so time-of-day is deliberately
omitted (the model can run `date` when it needs the exact wall clock).

### Per provider

- **Anthropic** (`AnthropicChat`): explicit ephemeral `cache_control`
  breakpoints on the three stable parts — system prompt, the last tool
  definition, and the last block of the last message (which advances as the
  history grows). Three of the four allowed breakpoints are used, default
  5-minute TTL. Usage reports `cache_creation_input_tokens` and
  `cache_read_input_tokens`, priced via `phoson_llm.pricing`.
- **OpenRouter** (`OpenRouterChat`): `ModelConfig.session_id` is sent as the
  top-level `session_id` body field, the sticky-routing key that pins the
  conversation to one upstream provider so its cache stays warm from the
  first turn. `anthropic/*` models additionally send the top-level
  `cache_control: {"type": "ephemeral"}` field to turn on automatic caching.
  Models with implicit caching (OpenAI, DeepSeek, Gemini 2.5+) need no
  client-side flag. The adapter identifies itself as *phoson-cli* in
  OpenRouter app rankings by default. Usage reports `prompt_tokens_details`
  (`cached_tokens`, `cache_write_tokens`).
- **OpenAI** (`OpenAIChat`): automatic caching is provider-side; the shared
  loop parses `prompt_tokens_details` into `cache_read` / `cache_write` and
  `calculate_cost` prices them when the model entry has cache rates.

### Metrics

`TokenUsage.cache_read` / `cache_write` are set on every `UsageEvent`
(`0` when the provider reports nothing). The CLI accumulates both across the
session (`SessionMetrics`) and shows them in `/status`
(`cache  R read / W write`) and `/tokens` (`cache=Rr/Ww`). Cached reads
typically cost 10–50% of base input price, so a warm cache cuts long-session
prompt cost by roughly 50–90%.

## Schemas

### Message

```python
from phoson_llm import Message

msg = Message(role="user", content="Hello!")
```

| Field     | Type                     | Description                          |
|-----------|--------------------------|--------------------------------------|
| `role`    | `Literal["system", "user", "assistant"]` | Message sender |
| `content` | `str \| list[ContentBlock]` | Message content                      |

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
| `session_id`       | `str \| None` | None    | Stable conversation key; OpenRouter uses it for sticky routing (prompt caching) |

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
    cache_write: int   # Cache write tokens (Anthropic / OpenAI prompt_tokens_details)
    cache_read: int   # Cache read tokens (Anthropic / OpenAI prompt_tokens_details)

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
# Returns: (0.015, True)  # (cost_usd, cost_known)
```

Use `PriceEntry` to get detailed pricing information:

```python
from phoson_llm import PriceEntry

entry = PriceEntry(input=2.50, output=10.00, cache_read=1.25)
print(entry.input)  # 2.50
print(entry.output)  # 10.00
print(entry.cache_read)  # 1.25
```

Note: `PriceEntry` is a frozen dataclass with fields `input`, `output`, `cache_write`, and `cache_read` (prices per million tokens). It does not take a `model` argument directly — use `calculate_cost()` to look up pricing for a specific model.

## Public API

```python
from phoson_llm import (
    # Chat adapters
    BaseLLMChat, OpenAIChat, AnthropicChat, OllamaChat, OpenRouterChat,
    GitHubModelsChat, NVIDIAChat, GrokChat, GroqChat, DeepSeekChat,
    TogetherChat, PerplexityChat, FireworksChat, CohereChat,
    LMStudioChat, VLLMChat, AzureChat,
    GeminiChat, MistralChat, BedrockChat,
    # Factory
    build_chat,
    # Schemas
    Message, TextBlock, ImageBlock, AudioBlock, VideoBlock, DocumentBlock,
    ToolDefinition, ToolUseBlock, ToolResultBlock, ModelConfig, ContentBlock,
    # Events
    LLMEvent, LLMStartEvent, LLMDoneEvent, TokenEvent,
    ReasoningStartEvent, ReasoningTokenEvent, ReasoningDoneEvent,
    ToolCallEvent, ToolCallDeltaEvent, UsageEvent, TokenUsage, ErrorEvent,
    # Pricing
    calculate_cost, PriceEntry,
)
```