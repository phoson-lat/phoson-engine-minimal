# AI Providers Expansion Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add support for 15+ new AI providers (OpenAI-compatible + native SDK) to `phoson_llm` with minimal boilerplate via a generic `OpenAICompatibleChat` class.

**Architecture:** Hybrid approach — a generic `OpenAICompatibleChat` class handles all OpenAI-compatible providers via configuration (`base_url`, `api_key_env`, `max_tokens_key`, `default_headers`, `extra_kwargs`); thin subclasses (~10 LOC each) provide named convenience classes for discoverability. Native SDK providers (Gemini, Mistral, Bedrock) get full custom `stream()` implementations with optional dependencies via extras. A top-level `build_chat(provider, **kwargs)` factory in `phoson_llm` replaces the CLI-only one.

**Tech Stack:** Python 3.12+, `openai` SDK (existing), `google-genai` (optional), `mistralai` (optional), `boto3` (optional), `httpx` (existing)

**Key design decisions:**
- One file per provider (user preference)
- Native SDKs as optional extras (`phoson[gemini]`, `phoson[aws]`, `phoson[mistral]`)
- `build_chat()` in `phoson_llm` (new general factory) + update existing `phoson_cli.config.build_chat`
- Pricing entries added per-provider as we go

---

## File Structure

### New files to create:
```
phoson_llm/
  ├── factory.py                      # build_chat factory function
  └── chats/
      ├── openai_compatible.py        # OpenAICompatibleChat generic class
      ├── github_models.py            # GitHubModelsChat
      ├── nvidia.py                   # NVIDIAChat
      ├── grok.py                     # xAI GrokChat
      ├── groq.py                     # GroqChat
      ├── deepseek.py                 # DeepSeekChat
      ├── together.py                 # TogetherChat
      ├── perplexity.py               # PerplexityChat
      ├── lmstudio.py                 # LMStudioChat
      ├── vllm.py                     # VLLMChat
      ├── azure.py                    # AzureChat
      ├── gemini.py                   # GeminiChat (native, optional dep)
      ├── mistral.py                  # MistralChat (native, optional dep)
      ├── bedrock.py                  # BedrockChat (native, optional dep)
      ├── fireworks.py                # FireworksChat
      └── cohere.py                   # CohereChat

tests/phoson_llm/
  ├── test_openai_compatible_unit.py
  ├── test_github_models_unit.py
  ├── test_nvidia_unit.py
  ├── test_grok_unit.py
  ├── test_groq_unit.py
  ├── test_deepseek_unit.py
  ├── test_together_unit.py
  ├── test_perplexity_unit.py
  ├── test_lmstudio_unit.py
  ├── test_vllm_unit.py
  ├── test_azure_unit.py
  ├── test_gemini_unit.py
  ├── test_mistral_unit.py
  └── test_bedrock_unit.py
```

### Files to modify:
```
phoson_llm/__init__.py              # Export new classes + build_chat
phoson_llm/chats/__init__.py        # Export all new classes
phoson_llm/pricing.py               # Add pricing entries for new providers
phoson_cli/config.py                # Update PhosonConfig + build_chat + save_config
pyproject.toml                      # Add optional dependency groups
docs/api/phoson_llm.md              # Document new providers
```

---

## Chunk 1: OpenAICompatibleChat + build_chat Infrastructure

Core dependency for all OpenAI-compatible providers. Must be done first.

### Task 1.1: Create OpenAICompatibleChat class

**Files:**
- Create: `phoson_llm/chats/openai_compatible.py`
- Create: `tests/phoson_llm/test_openai_compatible_unit.py`
- Read: `phoson_llm/chats/openai.py` (reference pattern)
- Read: `phoson_llm/chats/_openai_compatible.py:337-498` (stream_chat_completions API)

**Tests** (`test_openai_compatible_unit.py`):

```python
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat
from phoson_llm.chats.base import BaseLLMChat

DEFAULT_URL = "https://api.example.com/v1"

def test_is_base_llm_chat_subclass():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert isinstance(chat, BaseLLMChat)

def test_default_base_url():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._base_url == DEFAULT_URL

def test_custom_max_tokens_key():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key",
                                max_tokens_key="max_completion_tokens")
    assert chat._max_tokens_key == "max_completion_tokens"

def test_default_max_tokens_key_is_max_tokens():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._max_tokens_key == "max_tokens"

def test_client_uses_base_url():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert str(chat._client.base_url) == DEFAULT_URL + "/"

def test_client_uses_api_key():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="sk-test-123")
    assert chat._client.api_key == "sk-test-123"

def test_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "env-key-value")
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key_env="MY_CUSTOM_KEY")
    assert chat._client.api_key == "env-key-value"

def test_default_headers_are_passed():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key",
                                default_headers={"X-Custom": "value"})
    headers = getattr(chat._client, "default_headers", {})
    assert headers.get("X-Custom") == "value"

def test_default_headers_is_none_when_not_provided():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    headers = getattr(chat._client, "default_headers", None)
    assert headers is None

def test_cost_calculator_defaults_to_none():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._cost_calculator is None

def test_extra_kwargs_defaults_to_none():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._extra_kwargs is None

def test_repr_includes_provider_name():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key",
                                provider_name="MyProvider")
    assert "MyProvider" in repr(chat)

def test_repr_defaults_to_openai_compatible():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert "OpenAICompatible" in repr(chat)
```

**Implementation** (`openai_compatible.py`):

```python
"""Generic adapter for any OpenAI-compatible Chat Completions API."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats._openai_compatible import stream_chat_completions
from phoson_llm.schemas import Message, LLMEvent, ModelConfig, ToolDefinition


class OpenAICompatibleChat(BaseLLMChat):
    """Generic adapter for OpenAI-compatible Chat Completions APIs.

    Args:
        base_url: The provider's API base URL.
        api_key: API key. Falls back to ``api_key_env`` env var.
        api_key_env: Env var name for API key (default ``"API_KEY"``).
        max_tokens_key: ``"max_tokens"`` or ``"max_completion_tokens"``.
        default_headers: Optional HTTP headers sent with every request.
        cost_calculator: Optional cost callback. ``None`` = unknown cost.
        extra_kwargs: Extra kwargs for ``chat.completions.create``.
        provider_name: Human-readable name for ``repr()``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str = "API_KEY",
        max_tokens_key: str = "max_tokens",
        default_headers: dict[str, str] | None = None,
        cost_calculator: Any | None = None,
        extra_kwargs: dict[str, Any] | None = None,
        provider_name: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_tokens_key = max_tokens_key
        self._cost_calculator = cost_calculator
        self._extra_kwargs = extra_kwargs
        self._provider_name = provider_name or "OpenAICompatible"

        resolved_key = api_key or os.environ.get(api_key_env) or ""
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            default_headers=default_headers or None,
        )

    def __repr__(self) -> str:
        return f"{self._provider_name}Chat(base_url={self._base_url!r})"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        async for event in stream_chat_completions(
            self._client,
            messages=messages,
            config=config,
            tools=tools,
            max_tokens_key=self._max_tokens_key,
            cost_calculator=self._cost_calculator,
            extra_kwargs=self._extra_kwargs,
        ):
            yield event
```

**Steps:**
- [ ] Write `tests/phoson_llm/test_openai_compatible_unit.py`
- [ ] Run: `python -m pytest tests/phoson_llm/test_openai_compatible_unit.py -v` (expect FAIL)
- [ ] Write `phoson_llm/chats/openai_compatible.py`
- [ ] Run: `python -m pytest tests/phoson_llm/test_openai_compatible_unit.py -v` (expect PASS)
- [ ] Export from `phoson_llm/chats/__init__.py`
- [ ] Export from `phoson_llm/__init__.py`
- [ ] Commit

### Task 1.2: Create build_chat factory

**Files:**
- Create: `phoson_llm/factory.py`
- Modify: `phoson_llm/__init__.py`
- Modify: `tests/phoson_llm/test_openai_compatible_unit.py`

**Tests** (add to test file):

```python
from phoson_llm import build_chat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.exceptions import PhosonLLMError
import pytest

def test_build_chat_openai():
    chat = build_chat("openai", api_key="test-key")
    assert isinstance(chat, OpenAIChat)

def test_build_chat_anthropic():
    chat = build_chat("anthropic", api_key="test-key")
    assert isinstance(chat, AnthropicChat)

def test_build_chat_openrouter():
    chat = build_chat("openrouter", api_key="test-key")
    assert isinstance(chat, OpenRouterChat)

def test_build_chat_ollama():
    chat = build_chat("ollama")
    assert isinstance(chat, OllamaChat)

def test_build_chat_unknown_raises():
    with pytest.raises(PhosonLLMError, match="Unknown provider"):
        build_chat("nonexistent_provider")
```

**Implementation** (`phoson_llm/factory.py`):

```python
"""Provider factory — maps provider names to chat adapter instances."""

from __future__ import annotations

from typing import Any

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openrouter import OpenRouterChat
from phoson_llm.exceptions import PhosonLLMError


def build_chat(
    provider: str,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> BaseLLMChat:
    """Build an LLM chat adapter by provider name.

    Args:
        provider: Provider name.
        api_key: API key (defaults to the provider's env var).
        base_url: Optional base URL override.
        **kwargs: Extra constructor arguments.

    Returns:
        An instance of the appropriate ``BaseLLMChat`` subclass.

    Raises:
        PhosonLLMError: If the provider name is unknown.
    """
    _PROVIDERS: dict[str, type[BaseLLMChat]] = {
        "openai": OpenAIChat,
        "anthropic": AnthropicChat,
        "ollama": OllamaChat,
        "openrouter": OpenRouterChat,
    }

    cls = _PROVIDERS.get(provider.lower())
    if cls is None:
        raise PhosonLLMError(
            f"Unknown provider: {provider!r}. "
            f"Available: {', '.join(sorted(_PROVIDERS))}"
        )

    init_kwargs: dict[str, Any] = {}
    if api_key is not None:
        init_kwargs["api_key"] = api_key
    if base_url is not None:
        init_kwargs["base_url"] = base_url
    init_kwargs.update(kwargs)

    return cls(**init_kwargs)
```

**Steps:**
- [ ] Write factory tests
- [ ] Run tests (expect FAIL)
- [ ] Write `phoson_llm/factory.py`
- [ ] Export `build_chat` from `phoson_llm/__init__.py`
- [ ] Add `from phoson_llm.factory import build_chat` to `phoson_llm/__init__.py`
- [ ] Add `"build_chat"` to `__all__`
- [ ] Run all tests (expect PASS)
- [ ] Commit: `git commit -m "feat(llm): add OpenAICompatibleChat and build_chat factory"`

---

## Chunk 2: Priority OpenAI-Compatible Providers

Three providers the user explicitly prioritized.

### Task 2.1: GitHubModelsChat

**Files:**
- Create: `phoson_llm/chats/github_models.py`
- Create: `tests/phoson_llm/test_github_models_unit.py`

**Tests:**

```python
from phoson_llm.chats.github_models import GitHubModelsChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat
from phoson_llm.chats.base import BaseLLMChat

GITHUB_BASE = "https://models.inference.ai.azure.com"

def test_is_openai_compatible_subclass():
    chat = GitHubModelsChat(api_key="test-token")
    assert isinstance(chat, OpenAICompatibleChat)
    assert isinstance(chat, BaseLLMChat)

def test_default_base_url():
    chat = GitHubModelsChat(api_key="test-token")
    assert chat._base_url == GITHUB_BASE

def test_falls_back_to_github_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    chat = GitHubModelsChat()
    assert chat._client.api_key == "env-token"

def test_repr_includes_github_models():
    chat = GitHubModelsChat(api_key="test-token")
    assert "GitHubModels" in repr(chat)
```

**Implementation** (`github_models.py`):

```python
"""GitHub Models adapter. Uses the ``GITHUB_TOKEN`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

GITHUB_MODELS_BASE_URL = "https://models.inference.ai.azure.com"
ENV_VAR = "GITHUB_TOKEN"


class GitHubModelsChat(OpenAICompatibleChat):
    """Adapter for GitHub Models (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            base_url=GITHUB_MODELS_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="GitHubModels",
        )
```

**Steps:**
- [ ] Write tests
- [ ] Run tests (expect FAIL)
- [ ] Write adapter
- [ ] Run tests (expect PASS)
- [ ] Export in both `__init__.py` files
- [ ] Register in `factory.py` (`"github": GitHubModelsChat`)

### Task 2.2: NVIDIAChat

**Files:**
- Create: `phoson_llm/chats/nvidia.py`
- Create: `tests/phoson_llm/test_nvidia_unit.py`

Same pattern as GitHubModelsChat with:
- `base_url = "https://integrate.api.nvidia.com/v1"`
- `api_key_env = "NVIDIA_API_KEY"`
- `provider_name = "NVIDIA"`

**Steps:** Same as 2.1

### Task 2.3: GrokChat (xAI)

**Files:**
- Create: `phoson_llm/chats/grok.py`
- Create: `tests/phoson_llm/test_grok_unit.py`

Same pattern with:
- `base_url = "https://api.x.ai/v1"`
- `api_key_env = "XAI_API_KEY"`
- `provider_name = "Grok"`

**Steps:** Same as 2.1

### Task 2.4: Pricing + Commit

Add to `phoson_llm/pricing.py`:

```python
# ── xAI (Grok) ────────────────────────────────────────────────────────────
"xai/grok-3": PriceEntry(input=3.00, output=15.00),
"xai/grok-3-mini": PriceEntry(input=0.30, output=0.60),

# ── NVIDIA ─────────────────────────────────────────────────────────────────
"nvidia/llama-3.1-nemotron": PriceEntry(input=0.10, output=0.40),
```

- [ ] Add pricing entries
- [ ] Register all three in `factory.py`, `chats/__init__.py`, `phoson_llm/__init__.py`
- [ ] Run full test suite
- [ ] Commit: `git commit -m "feat(llm): add GitHubModels, NVIDIA, and Grok providers"`

---

## Chunk 3: More OpenAI-Compatible Providers

Four more providers following the identical thin subclass pattern.

### Task 3.1: GroqChat

- `base_url = "https://api.groq.com/openai/v1"`
- `api_key_env = "GROQ_API_KEY"`

### Task 3.2: DeepSeekChat

- `base_url = "https://api.deepseek.com/v1"`
- `api_key_env = "DEEPSEEK_API_KEY"`

### Task 3.3: TogetherChat

- `base_url = "https://api.together.xyz/v1"`
- `api_key_env = "TOGETHER_API_KEY"`

### Task 3.4: PerplexityChat

- `base_url = "https://api.perplexity.ai"`
- `api_key_env = "PERPLEXITY_API_KEY"`

### Task 3.5: Pricing + Commit

```python
# ── DeepSeek ──────────────────────────────────────────────────────────────
"deepseek/deepseek-chat": PriceEntry(input=0.27, output=1.10),
"deepseek/deepseek-reasoner": PriceEntry(input=0.55, output=2.19),

# ── Groq ──────────────────────────────────────────────────────────────────
"groq/llama-3.3-70b": PriceEntry(input=0.59, output=0.79),

# ── Together AI ───────────────────────────────────────────────────────────
"together/llama-3.3-70b": PriceEntry(input=0.72, output=0.72),

# ── Perplexity ────────────────────────────────────────────────────────────
"perplexity/sonar-pro": PriceEntry(input=1.00, output=5.00),
"perplexity/sonar": PriceEntry(input=0.20, output=1.00),
```

Each task follows: write test → run (FAIL) → write adapter → run (PASS) → export → register.

- [ ] GroqChat (tests + adapter)
- [ ] DeepSeekChat (tests + adapter)
- [ ] TogetherChat (tests + adapter)
- [ ] PerplexityChat (tests + adapter)
- [ ] Add pricing entries
- [ ] Run full suite
- [ ] Commit: `git commit -m "feat(llm): add Groq, DeepSeek, Together, Perplexity providers"`

---

## Chunk 4: Local OpenAI-Compatible Providers

Zero-config providers for local development.

### Task 4.1: LMStudioChat

**Files:**
- Create: `phoson_llm/chats/lmstudio.py`
- Create: `tests/phoson_llm/test_lmstudio_unit.py`

**Tests:**

```python
from phoson_llm.chats.lmstudio import LMStudioChat, LMSTUDIO_DEFAULT_BASE_URL
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_default_base_url():
    chat = LMStudioChat()
    assert chat._base_url == "http://localhost:1234/v1"

def test_custom_base_url():
    chat = LMStudioChat(base_url="http://192.168.1.5:1234/v1")
    assert chat._base_url == "http://192.168.1.5:1234/v1"

def test_is_openai_compatible_subclass():
    chat = LMStudioChat()
    assert isinstance(chat, OpenAICompatibleChat)

def test_repr_includes_lmstudio():
    chat = LMStudioChat()
    assert "LMStudio" in repr(chat)
```

**Implementation:**

```python
"""LM Studio local inference adapter."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

LMSTUDIO_DEFAULT_BASE_URL = "http://localhost:1234/v1"


class LMStudioChat(OpenAICompatibleChat):
    """Adapter for LM Studio local inference server.

    Args:
        base_url: LM Studio endpoint (default ``http://localhost:1234/v1``).
    """

    def __init__(self, base_url: str = LMSTUDIO_DEFAULT_BASE_URL) -> None:
        super().__init__(
            base_url=base_url,
            api_key="not-needed",
            provider_name="LMStudio",
        )
```

### Task 4.2: VLLMChat

**Files:**
- Create: `phoson_llm/chats/vllm.py`
- Create: `tests/phoson_llm/test_vllm_unit.py`

Same pattern with:
- Default `base_url = "http://localhost:8000/v1"`
- Has optional `api_key` (vLLM can be deployed with auth)
- `api_key_env = "VLLM_API_KEY"`

- [ ] LMStudioChat (tests + adapter)
- [ ] VLLMChat (tests + adapter)
- [ ] Export + register
- [ ] Commit: `git commit -m "feat(llm): add LM Studio and vLLM local providers"`

---

## Chunk 5: AzureChat (Enterprise)

### Task 5.1: Write tests

Azure is unique because it constructs the base URL from endpoint + deployment + api-version.

```python
from phoson_llm.chats.azure import AzureChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_is_openai_compatible_subclass():
    chat = AzureChat(
        azure_endpoint="https://my-resource.openai.azure.com",
        api_key="test-key",
        deployment="gpt-4o",
        api_version="2024-08-01-preview",
    )
    assert isinstance(chat, OpenAICompatibleChat)

def test_builds_correct_base_url():
    chat = AzureChat(
        azure_endpoint="https://my-resource.openai.azure.com",
        api_key="test-key",
        deployment="gpt-4o",
        api_version="2024-08-01-preview",
    )
    expected = ("https://my-resource.openai.azure.com/openai/deployments/"
                "gpt-4o/chat/completions?api-version=2024-08-01-preview")
    assert chat._base_url == expected

def test_uses_api_key():
    chat = AzureChat(
        azure_endpoint="https://my-resource.openai.azure.com",
        api_key="az-key-123",
        deployment="gpt-4o",
        api_version="2024-08-01-preview",
    )
    assert chat._client.api_key == "az-key-123"

def test_falls_back_to_env_vars(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env-resource.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key")
    chat = AzureChat()
    assert "env-resource" in chat._base_url
    assert "gpt-4o-mini" in chat._base_url
    assert chat._client.api_key == "env-key"
```

### Task 5.2: Write AzureChat

```python
"""Azure OpenAI adapter.

Constructs the endpoint URL from resource, deployment, and api-version.
"""

from __future__ import annotations

import os

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

AZURE_API_VERSION = "2024-08-01-preview"
AZURE_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
AZURE_API_KEY_ENV = "AZURE_OPENAI_API_KEY"
AZURE_DEPLOYMENT_ENV = "AZURE_OPENAI_DEPLOYMENT"


class AzureChat(OpenAICompatibleChat):
    """Adapter for Azure OpenAI Service.

    Args:
        azure_endpoint: Resource URL (e.g. ``https://my-resource.openai.azure.com``).
            Defaults to ``AZURE_OPENAI_ENDPOINT`` env var.
        api_key: API key. Defaults to ``AZURE_OPENAI_API_KEY`` env var.
        deployment: Deployment name. Defaults to ``AZURE_OPENAI_DEPLOYMENT`` env var.
        api_version: API version (default ``2024-08-01-preview``).
    """

    def __init__(
        self,
        azure_endpoint: str | None = None,
        api_key: str | None = None,
        deployment: str | None = None,
        api_version: str = AZURE_API_VERSION,
    ) -> None:
        endpoint = (azure_endpoint or os.environ.get(AZURE_ENDPOINT_ENV) or "").rstrip("/")
        dep = deployment or os.environ.get(AZURE_DEPLOYMENT_ENV) or ""
        key = api_key or os.environ.get(AZURE_API_KEY_ENV) or ""

        base_url = (
            f"{endpoint}/openai/deployments/{dep}"
            f"/chat/completions?api-version={api_version}"
        )

        super().__init__(
            base_url=base_url,
            api_key=key,
            max_tokens_key="max_tokens",
            default_headers={"api-key": key},
            provider_name="Azure",
        )
```

- [ ] Write tests
- [ ] Run tests (expect FAIL)
- [ ] Write adapter
- [ ] Run tests (expect PASS)
- [ ] Export + register + commit

---

## Chunk 6: Native SDK — GeminiChat

### Task 6.1: Add optional dependency

In `pyproject.toml`:

```toml
[project.optional-dependencies]
gemini = ["google-genai>=1.0.0"]
```

### Task 6.2: Write tests

```python
import pytest
pytest.importorskip("google.genai")

from phoson_llm.chats.gemini import GeminiChat
from phoson_llm.chats.base import BaseLLMChat

def test_is_base_llm_chat_subclass():
    chat = GeminiChat(api_key="test-key")
    assert isinstance(chat, BaseLLMChat)

def test_default_api_key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    chat = GeminiChat()
    assert chat._api_key == "env-key"

def test_repr_includes_gemini():
    chat = GeminiChat(api_key="test-key")
    assert "Gemini" in repr(chat)
```

### Task 6.3: Write GeminiChat

Full native implementation using `google.genai` SDK. Key points:
- Lazy import the SDK (not at module level, inside `__init__`)
- Convert Phoson messages to Gemini `Content`/`Part` objects
- Handle streaming via `generate_content_stream`
- Extract text tokens, function calls, and reasoning
- Map usage for cost calculation (pricing already exists in `pricing.py`)

See the full implementation in the `phoson_llm/chats/gemini.py` spec below.

The adapter should implement `convert_messages` and `convert_tools` helper functions similar to how AnthropicChat and OllamaChat do it.

**Steps:**
- [ ] Add `google-genai` to `[project.optional-dependencies] gemini`
- [ ] Write tests
- [ ] Run tests (expect FAIL — module not found)
- [ ] Write adapter
- [ ] Run tests (expect PASS or skip if SDK missing)
- [ ] Export + register
- [ ] Commit

---

## Chunk 7: Native SDK — MistralChat + BedrockChat

### Task 7.1: Add optional deps

In `pyproject.toml`:

```toml
[project.optional-dependencies]
mistral = ["mistralai>=1.0.0"]
aws = ["boto3>=1.35.0"]
```

### Task 7.2: MistralChat

Mistral has a native SDK (`mistralai`). Implementation follows the same pattern as AnthropicChat:
- `mistralai.Mistral` client
- `chat.stream_async()` for token streaming
- Convert Phoson messages to Mistral format
- Handle tool calls from stream chunks

**Files:**
- `phoson_llm/chats/mistral.py`
- `tests/phoson_llm/test_mistral_unit.py`

### Task 7.3: BedrockChat

AWS Bedrock uses `boto3` with the Converse API. Since `boto3` is synchronous, we use `converse()` (non-streaming) to start, emitting the response as events. Streaming via `converse_stream()` can be added later.

**Files:**
- `phoson_llm/chats/bedrock.py`
- `tests/phoson_llm/test_bedrock_unit.py`

### Task 7.4: Pricing

Add entries:
```python
# ── Mistral ───────────────────────────────────────────────────────────────
"mistral/mistral-large-latest": PriceEntry(input=2.00, output=6.00),
"mistral/mistral-small-latest": PriceEntry(input=0.50, output=1.50),
"mistral/codestral-latest": PriceEntry(input=1.00, output=3.00),

# ── AWS Bedrock ───────────────────────────────────────────────────────────
"bedrock/claude-sonnet-4-6": PriceEntry(input=3.00, output=15.00),
"bedrock/claude-haiku-4-5": PriceEntry(input=1.00, output=5.00),
```

**Steps per provider:** write test → run (FAIL) → write adapter → run (PASS) → export → register
- [ ] MistralChat tests + adapter
- [ ] BedrockChat tests + adapter
- [ ] Pricing entries
- [ ] Export + register + commit

---

## Chunk 8: Secondary Providers + Pricing

### Task 8.1-2: FireworksChat + CohereChat

Both are OpenAI-compatible thin subclasses:

| Provider | base_url | env_var |
|---|---|---|
| Fireworks | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| Cohere | `https://api.cohere.com/v1` | `COHERE_API_KEY` |

### Task 8.3: Cohere native support

Cohere also has a native SDK. For this plan, the OpenAI-compatible endpoint covers the basic use case. If needed, a native implementation can be added later.

### Task 8.4: Pricing

```python
# ── Fireworks ─────────────────────────────────────────────────────────────
"fireworks/llama-3.3-70b": PriceEntry(input=0.50, output=0.50),

# ── Cohere ────────────────────────────────────────────────────────────────
"cohere/command-r-plus": PriceEntry(input=2.50, output=10.00),
"cohere/command-r": PriceEntry(input=0.50, output=1.50),
```

- [ ] FireworksChat (tests + adapter)
- [ ] CohereChat (tests + adapter)
- [ ] Pricing entries
- [ ] Export + register + commit

---

## Chunk 9: CLI Integration — PhosonConfig + build_chat

### Task 9.1: Add new fields to PhosonConfig

Add to `phoson_llm/cli/config.py`:

```python
@dataclass
class PhosonConfig:
    # ...existing fields...
    github_token: str | None = None
    nvidia_api_key: str | None = None
    xai_api_key: str | None = None
    groq_api_key: str | None = None
    deepseek_api_key: str | None = None
    together_api_key: str | None = None
    perplexity_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None
    gemini_api_key: str | None = None
    mistral_api_key: str | None = None
    fireworks_api_key: str | None = None
    cohere_api_key: str | None = None
    vllm_base_url: str | None = None
    vllm_api_key: str | None = None
    lmstudio_base_url: str | None = None
```

### Task 9.2: Update load_config()

Add env var resolution for each new field in `load_config()`.

### Task 9.3: Update enabled_providers_from_config()

Add detection for each new provider.

### Task 9.4: Update build_chat() in phoson_cli/config.py

Add cases for all new providers, importing the adapter classes and constructing them from config fields.

### Task 9.5: Update save_config()

Serialize the new fields to the TOML config file.

### Task 9.6: Update tests

Update `tests/phoson_cli/` tests to cover the new providers.

- [ ] Update PhosonConfig dataclass
- [ ] Update load_config()
- [ ] Update enabled_providers_from_config()
- [ ] Update build_chat() in phoson_cli
- [ ] Update save_config()
- [ ] Run `python -m pytest tests/phoson_cli -v`
- [ ] Commit

---

## Chunk 10: Documentation + Lint + Verify

### Task 10.1: Update docs

Update `docs/api/phoson_llm.md`:
- Add `OpenAICompatibleChat` section
- Add `build_chat()` section
- Add each new provider in the chat adapters list
- Document env vars and examples

### Task 10.2: Run lint

```bash
python -m ruff check phoson_llm phoson_cli tests/phoson_llm tests/phoson_cli
```

Fix any issues found.

### Task 10.3: Run type check

```bash
python -m pyright phoson_llm phoson_cli
```

Fix any type errors.

### Task 10.4: Run all tests

```bash
python -m pytest tests/phoson_llm tests/phoson_cli -v
```

Expected: all tests PASS.

### Task 10.5: Final commit

- [ ] Update docs
- [ ] Run lint (fix issues)
- [ ] Run type check (fix issues)
- [ ] Run tests (all pass)
- [ ] Commit: `git commit -m "docs: update provider documentation and run quality checks"`

---

## Summary: All 15 New Providers

| # | Provider | Class | File | Type | Env Var |
|---|---|---|---|---|---|
| 1 | GitHub Models | `GitHubModelsChat` | `github_models.py` | OpenAI-comp | `GITHUB_TOKEN` |
| 2 | NVIDIA NIM | `NVIDIAChat` | `nvidia.py` | OpenAI-comp | `NVIDIA_API_KEY` |
| 3 | xAI (Grok) | `GrokChat` | `grok.py` | OpenAI-comp | `XAI_API_KEY` |
| 4 | Groq | `GroqChat` | `groq.py` | OpenAI-comp | `GROQ_API_KEY` |
| 5 | DeepSeek | `DeepSeekChat` | `deepseek.py` | OpenAI-comp | `DEEPSEEK_API_KEY` |
| 6 | Together AI | `TogetherChat` | `together.py` | OpenAI-comp | `TOGETHER_API_KEY` |
| 7 | Perplexity | `PerplexityChat` | `perplexity.py` | OpenAI-comp | `PERPLEXITY_API_KEY` |
| 8 | LM Studio | `LMStudioChat` | `lmstudio.py` | OpenAI-comp | — (configurable URL) |
| 9 | vLLM | `VLLMChat` | `vllm.py` | OpenAI-comp | `VLLM_API_KEY` |
| 10 | Azure OpenAI | `AzureChat` | `azure.py` | OpenAI-comp* | `AZURE_OPENAI_*` |
| 11 | Google Gemini | `GeminiChat` | `gemini.py` | Native SDK | `GEMINI_API_KEY` |
| 12 | Mistral AI | `MistralChat` | `mistral.py` | Native SDK | `MISTRAL_API_KEY` |
| 13 | AWS Bedrock | `BedrockChat` | `bedrock.py` | Native SDK | `AWS_*` |
| 14 | Fireworks AI | `FireworksChat` | `fireworks.py` | OpenAI-comp | `FIREWORKS_API_KEY` |
| 15 | Cohere | `CohereChat` | `cohere.py` | OpenAI-comp | `COHERE_API_KEY` |

*Azure is OpenAI-compatible but with a custom URL construction pattern.
