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
"""

from phoson_llm.chats import (
    OllamaChat,
    OpenAIChat,
    BaseLLMChat,
    AnthropicChat,
    OpenRouterChat,
    OpenAICompatibleChat,
    GitHubModelsChat,
    NVIDIAChat,
    GrokChat,
    GroqChat,
    DeepSeekChat,
    TogetherChat,
    PerplexityChat,
    LMStudioChat,
    VLLMChat,
    AzureChat,
    GeminiChat,
    MistralChat,
    BedrockChat,
)
from phoson_llm.factory import build_chat
from phoson_llm.pricing import PriceEntry, calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    AudioBlock,
    ErrorEvent,
    # multimodal
    ImageBlock,
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
    "build_chat",
    # schemas - inputs
    "Message",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ContentBlock",
    "ToolDefinition",
    "ModelConfig",
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
