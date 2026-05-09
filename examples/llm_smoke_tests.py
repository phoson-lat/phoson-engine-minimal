"""Smoke tests / usage examples for `phoson_llm` adapters.

This script is **not** part of the public API. It exercises each provider
adapter end-to-end and renders the streamed events to stdout. Use it as a
quick sanity check after touching any adapter.

Required environment variables (only for the providers you enable):
    - ANTHROPIC_API_KEY
    - OPENAI_API_KEY
    - OPENROUTER_API_KEY
    - OLLAMA_BASE_URL  (optional, defaults to http://localhost:11434)

Usage:
    python examples/llm_smoke_tests.py
"""

import os
import asyncio

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ReasoningDoneEvent,
    ToolCallDeltaEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat


async def render(
    label: str,
    chat: BaseLLMChat,
    messages: list[Message],
    config: ModelConfig,
    tools: list[ToolDefinition] | None = None,
) -> None:
    """Run a chat and pretty-print every streamed event."""
    print(f"\n{'─' * 60}\n  {label}\n{'─' * 60}")

    async for event in chat.stream(messages, config, tools):
        match event:
            case LLMStartEvent():
                print(f"[start] model={event.model} msgs={event.message_count}")
            case ReasoningStartEvent():
                print("[reasoning] ", end="", flush=True)
            case ReasoningTokenEvent():
                print(event.content, end="", flush=True)
            case ReasoningDoneEvent():
                print(f"\n[reasoning done] {len(event.content)} chars")
            case TokenEvent():
                print(event.content, end="", flush=True)
            case ToolCallDeltaEvent():
                if event.args_chunk:
                    print(event.args_chunk, end="", flush=True)
            case ToolCallEvent():
                print(f"\n[tool_call] #{event.index} {event.tool_name}({event.args})")
            case UsageEvent():
                u = event.usage
                known = "known" if event.cost_known else "unknown (local model)"
                print(
                    f"\n[usage] in={u.input} out={u.output} "
                    f"cache_write={u.cache_write} cache_read={u.cache_read} "
                    f"cost=${event.cost_usd:.6f} ({known})"
                )
            case LLMDoneEvent():
                print(f"\n[done] tool_calls={event.has_tool_calls}")
            case ErrorEvent():
                print(
                    f"\n[error] {event.code}: {event.message} "
                    f"retryable={event.retryable}"
                )


WEATHER_TOOL = ToolDefinition(
    name="get_weather",
    description="Gets the current weather of a city.",
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "Name of the city"},
            "country": {"type": "string", "description": "ISO country code"},
        },
        "required": ["city"],
    },
)


async def test_anthropic_basic() -> None:
    chat = AnthropicChat()
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="claude-haiku-4-5", max_tokens=256)
    await render("Anthropic — basic text", chat, messages, config)


async def test_anthropic_thinking() -> None:
    chat = AnthropicChat()
    messages = [
        Message(role="user", content="How many r's are in the word 'strawberry'?")
    ]
    config = ModelConfig(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        thinking_budget=2048,
    )
    await render("Anthropic — extended thinking", chat, messages, config)


async def test_anthropic_tools() -> None:
    chat = AnthropicChat()
    messages = [Message(role="user", content="What is the weather in Querétaro?")]
    config = ModelConfig(model="claude-haiku-4-5", max_tokens=512)
    await render("Anthropic — tool call", chat, messages, config, [WEATHER_TOOL])


async def test_openrouter_basic() -> None:
    chat = OpenRouterChat()  # reads OPENROUTER_API_KEY from env
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=256)
    await render("OpenRouter — basic text", chat, messages, config)


async def test_openrouter_tools() -> None:
    chat = OpenRouterChat()
    messages = [Message(role="user", content="What is the weather in Querétaro?")]
    config = ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=512)
    await render("OpenRouter — tool call", chat, messages, config, [WEATHER_TOOL])


async def test_openai_basic() -> None:
    chat = OpenAIChat()  # reads OPENAI_API_KEY from env
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="gpt-4o-mini", max_tokens=256)
    await render("OpenAI — basic text", chat, messages, config)


async def test_ollama_basic() -> None:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    chat = OllamaChat(base_url=base_url)
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="llama3.2", max_tokens=256)
    await render("Ollama — basic text (local)", chat, messages, config)


# Toggle the providers you want to exercise. Defaults to none so the script
# is safe to run without API keys.
ENABLED: dict[str, bool] = {
    "anthropic_basic": False,
    "anthropic_thinking": False,
    "anthropic_tools": False,
    "openai_basic": False,
    "openrouter_basic": False,
    "openrouter_tools": False,
    "ollama_basic": False,
}


TESTS = {
    "anthropic_basic": test_anthropic_basic,
    "anthropic_thinking": test_anthropic_thinking,
    "anthropic_tools": test_anthropic_tools,
    "openai_basic": test_openai_basic,
    "openrouter_basic": test_openrouter_basic,
    "openrouter_tools": test_openrouter_tools,
    "ollama_basic": test_ollama_basic,
}


async def main() -> None:
    selected = [name for name, enabled in ENABLED.items() if enabled]
    if not selected:
        print(
            "No tests enabled. Edit ENABLED in this file to toggle providers."
        )
        return
    for name in selected:
        try:
            await TESTS[name]()
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"\n[FAIL] {name}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
