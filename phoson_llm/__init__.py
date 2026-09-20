"""
Phoson LLM - Unified LLM chat interface.

This module provides a unified interface for interacting with various LLM providers
(OpenAI, Anthropic, Ollama, OpenRouter) with support for streaming, tools, and
multimodal inputs.

Example:
    >>> from phoson_llm import OpenAIChat, ModelConfig, Message
    >>>
    >>> chat = OpenAIChat(api_key="sk-...")
    >>> config = ModelConfig(model="gpt-4o", max_tokens=1024)
    >>>
    >>> messages = [Message(role="user", content="Hello!")]
    >>> async for event in chat.stream(messages, config):
    ...     print(event)

Import cost note
----------------
The provider adapters (``OpenAIChat``, ``AnthropicChat``, …) are exposed
lazily through :func:`__getattr__` (PEP 562). Importing this package — or
any of its cheap submodules such as ``phoson_llm.schemas`` — therefore no
longer imports the ``openai`` / ``anthropic`` SDKs; the SDK for a given
provider is loaded only when its adapter class is first accessed.
"""

from importlib import import_module

from phoson_llm.factory import build_chat
from phoson_llm.pricing import PriceEntry, calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    JsonValue,
    TextBlock,
    AudioBlock,
    ErrorEvent,
    # multimodal
    ImageBlock,
    JsonObject,
    JsonSchema,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    ContentBlock,
    LLMDoneEvent,
    ToolUseBlock,
    DocumentBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningDoneEvent,
    ToolCallDeltaEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.exceptions import (
    PhosonLLMError,
    PhosonProviderError,
    PhosonLLMProtocolError,
)

__all__ = [
    # chats
    "BaseLLMChat",
    "OpenAIChat",
    "AnthropicChat",
    "OllamaChat",
    "OpenRouterChat",
    "OpenAICompatibleChat",
    "GitHubModelsChat",
    "NVIDIAChat",
    "GrokChat",
    "GroqChat",
    "DeepSeekChat",
    "TogetherChat",
    "PerplexityChat",
    "LMStudioChat",
    "VLLMChat",
    "AzureChat",
    "GeminiChat",
    "MistralChat",
    "BedrockChat",
    "FireworksChat",
    "CohereChat",
    "OmniRouteChat",
    "build_chat",
    # schemas - inputs
    "Message",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ContentBlock",
    "ToolDefinition",
    "ModelConfig",
    # schemas - JSON aliases
    "JsonValue",
    "JsonObject",
    "JsonSchema",
    # schemas - multimodal inputs
    "ImageBlock",
    "AudioBlock",
    "VideoBlock",
    "DocumentBlock",
    # schemas - outputs
    "LLMEvent",
    "LLMStartEvent",
    "LLMDoneEvent",
    "TokenEvent",
    "ReasoningStartEvent",
    "ReasoningTokenEvent",
    "ReasoningDoneEvent",
    "ToolCallEvent",
    "ToolCallDeltaEvent",
    "TokenUsage",
    "UsageEvent",
    "ErrorEvent",
    # pricing
    "calculate_cost",
    "PriceEntry",
    # exceptions
    "PhosonLLMError",
    "PhosonLLMProtocolError",
    "PhosonProviderError",
]

#: Names re-exported from ``phoson_llm.chats`` — resolved lazily so the
#: vendor SDKs are not imported until the adapter is actually used.
_CHAT_EXPORTS = frozenset(
    {
        "BaseLLMChat",
        "OpenAIChat",
        "AnthropicChat",
        "OllamaChat",
        "OpenRouterChat",
        "OpenAICompatibleChat",
        "GitHubModelsChat",
        "NVIDIAChat",
        "GrokChat",
        "GroqChat",
        "DeepSeekChat",
        "TogetherChat",
        "PerplexityChat",
        "LMStudioChat",
        "VLLMChat",
        "AzureChat",
        "GeminiChat",
        "MistralChat",
        "BedrockChat",
        "FireworksChat",
        "CohereChat",
        "OmniRouteChat",
    }
)


def __getattr__(name: str):
    """Lazily re-export the chat adapters from ``phoson_llm.chats``."""
    if name in _CHAT_EXPORTS:
        value = getattr(import_module("phoson_llm.chats"), name)
        globals()[name] = value  # cache: subsequent accesses skip __getattr__
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
