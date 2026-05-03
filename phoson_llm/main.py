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
from phoson_llm.chats.anthropic import AnthropicChat

# ─── Event renderer ───────────────────────────────────────────────────────────


async def run(
    label: str,
    chat: BaseLLMChat,
    messages: list[Message],
    config: ModelConfig,
    tools: list[ToolDefinition] | None = None,
) -> None:
    """
    Executes a chat and renders events to the console.

    Args:
        label (str): Label for the test.
        chat (BaseLLMChat): Instance of the chat adapter.
        messages (list[Message]): List of messages.
        config (ModelConfig): Model configuration.
        tools (list[ToolDefinition] | None): Optional tools.
    """
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")

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


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_anthropic_basic() -> None:
    """Basic test with Anthropic."""
    chat = AnthropicChat()
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="claude-haiku-4-5", max_tokens=256)
    await run("Anthropic — basic text", chat, messages, config)


async def test_anthropic_thinking() -> None:
    """Extended thinking test with Anthropic."""
    chat = AnthropicChat()
    messages = [
        Message(role="user", content="How many r's are in the word 'strawberry'?")
    ]
    config = ModelConfig(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        thinking_budget=2048,
    )
    await run("Anthropic — extended thinking", chat, messages, config)


async def test_anthropic_tools() -> None:
    """Tool use test with Anthropic."""
    chat = AnthropicChat()
    messages = [Message(role="user", content="What is the weather in Querétaro?")]
    config = ModelConfig(model="claude-haiku-4-5", max_tokens=512)
    tools = [
        ToolDefinition(
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
    ]
    await run("Anthropic — tool call", chat, messages, config, tools)


async def test_openai_basic() -> None:
    """Basic test with OpenAI/OpenRouter."""
    chat = OpenAIChat(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-REDACTED",
    )
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=256)
    await run("OpenAI — basic text", chat, messages, config)


async def test_openai_tools() -> None:
    """Tool use test with OpenAI/OpenRouter."""
    chat = OpenAIChat(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-REDACTED",
    )
    messages = [Message(role="user", content="What is the weather in Querétaro?")]
    config = ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=512)
    tools = [
        ToolDefinition(
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
    ]
    await run("OpenAI — tool call", chat, messages, config, tools)


async def test_ollama_basic() -> None:
    """Basic test with local Ollama."""
    chat = OpenAIChat(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    )
    messages = [Message(role="user", content="Say hello in 3 different languages.")]
    config = ModelConfig(model="llama3.2", max_tokens=256)
    await run("Ollama — basic text (local)", chat, messages, config)


# ─── Entry point ──────────────────────────────────────────────────────────────


async def main() -> None:
    """Main entry point for tests."""
    tests = {
        "anthropic_basic": False,
        "anthropic_thinking": False,
        "anthropic_tools": False,
        "openai_basic": True,
        "openai_tools": True,
        "ollama_basic": False,
    }

    if tests["anthropic_basic"]:
        await test_anthropic_basic()

    if tests["anthropic_thinking"]:
        await test_anthropic_thinking()

    if tests["anthropic_tools"]:
        await test_anthropic_tools()

    if tests["openai_basic"]:
        await test_openai_basic()

    if tests["openai_tools"]:
        await test_openai_tools()

    if tests["ollama_basic"]:
        await test_ollama_basic()


if __name__ == "__main__":
    asyncio.run(main())
