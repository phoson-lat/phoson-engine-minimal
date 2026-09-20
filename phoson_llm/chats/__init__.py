"""Provider chat adapters.

Importing this package is deliberately cheap: adapters are imported lazily
on first attribute access (PEP 562) instead of eagerly here. Each adapter
module imports a heavy vendor SDK (``openai``, ``anthropic``, …), so pulling
all of them in at once used to cost ~1.5 s and ~30 MB of RSS on every
process that merely touched ``phoson_llm.schemas``. With ``__getattr__``
the SDK for the active provider is the only one loaded, and only when it is
actually constructed.
"""

from typing import TYPE_CHECKING
from importlib import import_module

if TYPE_CHECKING:
    # Static view of the lazily-resolved names: pyright sees them as module
    # members (so ``__all__`` and ``from phoson_llm.chats import X`` type-check)
    # while runtime still loads each on first access. Never executed.
    from phoson_llm.chats.base import BaseLLMChat
    from phoson_llm.chats.grok import GrokChat
    from phoson_llm.chats.groq import GroqChat
    from phoson_llm.chats.vllm import VLLMChat
    from phoson_llm.chats.azure import AzureChat
    from phoson_llm.chats.cohere import CohereChat
    from phoson_llm.chats.gemini import GeminiChat
    from phoson_llm.chats.nvidia import NVIDIAChat
    from phoson_llm.chats.ollama import OllamaChat
    from phoson_llm.chats.openai import OpenAIChat
    from phoson_llm.chats.bedrock import BedrockChat
    from phoson_llm.chats.mistral import MistralChat
    from phoson_llm.chats.deepseek import DeepSeekChat
    from phoson_llm.chats.lmstudio import LMStudioChat
    from phoson_llm.chats.together import TogetherChat
    from phoson_llm.chats.anthropic import AnthropicChat
    from phoson_llm.chats.fireworks import FireworksChat
    from phoson_llm.chats.omniroute import OmniRouteChat
    from phoson_llm.chats.openrouter import OpenRouterChat
    from phoson_llm.chats.perplexity import PerplexityChat
    from phoson_llm.chats.github_models import GitHubModelsChat
    from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

__all__ = [
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
]

#: public name → submodule that defines it (relative to this package).
_MODULES: dict[str, str] = {
    "BaseLLMChat": ".base",
    "OpenAIChat": ".openai",
    "AnthropicChat": ".anthropic",
    "OllamaChat": ".ollama",
    "OpenRouterChat": ".openrouter",
    "OpenAICompatibleChat": ".openai_compatible",
    "GitHubModelsChat": ".github_models",
    "NVIDIAChat": ".nvidia",
    "GrokChat": ".grok",
    "GroqChat": ".groq",
    "DeepSeekChat": ".deepseek",
    "TogetherChat": ".together",
    "PerplexityChat": ".perplexity",
    "LMStudioChat": ".lmstudio",
    "VLLMChat": ".vllm",
    "AzureChat": ".azure",
    "GeminiChat": ".gemini",
    "MistralChat": ".mistral",
    "BedrockChat": ".bedrock",
    "FireworksChat": ".fireworks",
    "CohereChat": ".cohere",
    "OmniRouteChat": ".omniroute",
}


def __getattr__(name: str):
    """Resolve a public adapter name to its submodule on first access."""
    submodule = _MODULES.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(submodule, __name__), name)
    globals()[name] = value  # cache: subsequent accesses skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
