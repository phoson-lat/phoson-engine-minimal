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

# ─── Renderer de eventos ──────────────────────────────────────────────────────


async def run(
    label: str,
    chat: BaseLLMChat,
    messages: list[Message],
    config: ModelConfig,
    tools: list[ToolDefinition] | None = None,
) -> None:
    """
    Ejecuta un chat y renderiza los eventos en la consola.

    Args:
        label (str): Etiqueta para el test.
        chat (BaseLLMChat): Instancia del adaptador de chat.
        messages (list[Message]): Lista de mensajes.
        config (ModelConfig): Configuración del modelo.
        tools (list[ToolDefinition] | None): Herramientas opcionales.
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
    """Test básico con Anthropic."""
    chat = AnthropicChat()
    messages = [Message(role="user", content="Di hola en 3 idiomas distintos.")]
    config = ModelConfig(model="claude-haiku-4-5", max_tokens=256)
    await run("Anthropic — texto básico", chat, messages, config)


async def test_anthropic_thinking() -> None:
    """Test de extended thinking con Anthropic."""
    chat = AnthropicChat()
    messages = [
        Message(role="user", content="¿Cuántos r tiene la palabra 'strawberry'?")
    ]
    config = ModelConfig(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        thinking_budget=2048,
    )
    await run("Anthropic — extended thinking", chat, messages, config)


async def test_anthropic_tools() -> None:
    """Test de tool use con Anthropic."""
    chat = AnthropicChat()
    messages = [Message(role="user", content="¿Qué clima hace en Querétaro?")]
    config = ModelConfig(model="claude-haiku-4-5", max_tokens=512)
    tools = [
        ToolDefinition(
            name="get_weather",
            description="Obtiene el clima actual de una ciudad.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Nombre de la ciudad"},
                    "country": {"type": "string", "description": "Código de país ISO"},
                },
                "required": ["city"],
            },
        )
    ]
    await run("Anthropic — tool call", chat, messages, config, tools)


async def test_openai_basic() -> None:
    """Test básico con OpenAI/OpenRouter."""
    chat = OpenAIChat(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-REDACTED",
    )
    messages = [Message(role="user", content="Di hola en 3 idiomas distintos.")]
    config = ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=256)
    await run("OpenAI — texto básico", chat, messages, config)


async def test_openai_tools() -> None:
    """Test de tool use con OpenAI/OpenRouter."""
    chat = OpenAIChat(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-REDACTED",
    )
    messages = [Message(role="user", content="¿Qué clima hace en Querétaro?")]
    config = ModelConfig(model="minimax/minimax-m2.5:free", max_tokens=512)
    tools = [
        ToolDefinition(
            name="get_weather",
            description="Obtiene el clima actual de una ciudad.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Nombre de la ciudad"},
                    "country": {"type": "string", "description": "Código de país ISO"},
                },
                "required": ["city"],
            },
        )
    ]
    await run("OpenAI — tool call", chat, messages, config, tools)


async def test_ollama_basic() -> None:
    """Test básico con Ollama local."""
    chat = OpenAIChat(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    )
    messages = [Message(role="user", content="Di hola en 3 idiomas distintos.")]
    config = ModelConfig(model="llama3.2", max_tokens=256)
    await run("Ollama — texto básico (local)", chat, messages, config)


# ─── Entry point ──────────────────────────────────────────────────────────────


async def main() -> None:
    """Punto de entrada principal para tests."""
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
