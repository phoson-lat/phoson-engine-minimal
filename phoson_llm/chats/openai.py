import os
import json
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, APIStatusError, APIConnectionError

from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    # inputs
    Message,
    # outputs
    LLMEvent,
    TextBlock,
    ErrorEvent,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningDoneEvent,
    ToolCallDeltaEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.chats.base import BaseLLMChat

# ─── Conversión de mensajes Phoson → OpenAI ───────────────────────────────────


def _convert_messages(messages: list[Message]) -> list[dict]:
    """
    Convierte el formato interno de Phoson al formato que espera OpenAI.

    Diferencias clave vs Anthropic:
    - system va como mensaje {"role": "system", "content": "..."}
    - ToolUseBlock  → mensaje role=assistant con tool_calls[]
    - ToolResultBlock → mensaje role=tool con tool_call_id
    """
    result = []

    for msg in messages:
        # System: OpenAI lo acepta como mensaje normal
        if msg.role == "system":
            content = msg.content if isinstance(msg.content, str) else ""
            result.append({"role": "system", "content": content})
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
            continue

        # content es lista de ContentBlocks — hay que separar por tipo
        text_blocks = [b for b in msg.content if isinstance(b, TextBlock)]
        tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
        tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]

        # Mensaje assistant con tool_calls
        if tool_uses:
            result.append(
                {
                    "role": "assistant",
                    "content": text_blocks[0].text if text_blocks else "",
                    "tool_calls": [
                        {
                            "id": b.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": b.tool_name,
                                "arguments": json.dumps(b.args),
                            },
                        }
                        for b in tool_uses
                    ],
                }
            )

        # Resultados de tools: cada uno es un mensaje role=tool separado
        for b in tool_results:
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": b.tool_call_id,
                    "content": b.result,
                }
            )

        # Mensaje de usuario puro (sin tool_uses ni tool_results)
        if not tool_uses and not tool_results and text_blocks:
            result.append(
                {
                    "role": msg.role,
                    "content": " ".join(b.text for b in text_blocks),
                }
            )

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Convierte ToolDefinition al formato de tools de OpenAI."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


# ─── Adapter ─────────────────────────────────────────────────────────────────


class OpenAIChat(BaseLLMChat):
    """
    Adapter para OpenAI Chat Completions API.
    Compatible también con Ollama y OpenRouter vía base_url.

    Ejemplos:
        OpenAIChat()                                    # OpenAI estándar
        OpenAIChat(base_url="http://localhost:11434/v1",
                   api_key="ollama")                    # Ollama local
        OpenAIChat(base_url="https://openrouter.ai/api/v1",
                   api_key=os.environ["OPENROUTER_API_KEY"])  # OpenRouter
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url,  # None = default OpenAI
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:

        # ── Construir kwargs del request ──────────────────────────────────────
        kwargs: dict = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "messages": _convert_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},  # usage en último chunk
        }

        # system desde config tiene prioridad sobre el mensaje system
        if config.system:
            kwargs["messages"].insert(
                0,
                {
                    "role": "system",
                    "content": config.system,
                },
            )

        if config.temperature is not None:
            kwargs["temperature"] = config.temperature

        if tools:
            kwargs["tools"] = _convert_tools(tools)

        # reasoning_effort para o1/o3 — incompatible con temperature
        if config.reasoning_effort:
            kwargs["reasoning_effort"] = config.reasoning_effort
            kwargs.pop("temperature", None)

        # ── Estado interno ────────────────────────────────────────────────────
        text_acc = ""
        reasoning_acc = ""
        tool_args_acc: dict[int, str] = {}  # index → partial JSON acumulado
        tool_names: dict[int, str] = {}  # index → nombre
        tool_ids: dict[int, str] = {}  # index → tool_call_id
        has_tool_calls = False
        final_usage = None
        tools_emitted = False

        # ── LLMStartEvent ─────────────────────────────────────────────────────
        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

        # ── Stream ────────────────────────────────────────────────────────────
        try:
            async for chunk in await self._client.chat.completions.create(**kwargs):
                # Último chunk vacío — solo trae el usage
                if not chunk.choices:
                    if chunk.usage:
                        final_usage = chunk.usage
                    continue

                delta = chunk.choices[0].delta

                # ── Texto ─────────────────────────────────────────────────────
                if delta.content:
                    text_acc += delta.content
                    yield TokenEvent(content=delta.content)

                # ── Reasoning (o1/o3 y OpenRouter con reasoning_content) ───────
                reasoning_chunk = getattr(delta, "reasoning_content", None)
                if reasoning_chunk:
                    if not reasoning_acc:
                        yield ReasoningStartEvent()
                    reasoning_acc += reasoning_chunk
                    yield ReasoningTokenEvent(content=reasoning_chunk)

                # ── Tool calls ────────────────────────────────────────────────
                if delta.tool_calls:
                    has_tool_calls = True
                    for tc in delta.tool_calls:
                        idx = tc.index

                        # Primer chunk del tool call — trae id y nombre
                        if tc.id:
                            tool_ids[idx] = tc.id
                            tool_names[idx] = tc.function.name or ""
                            tool_args_acc[idx] = ""

                        # Chunks de args (partial JSON)
                        if tc.function and tc.function.arguments:
                            chunk_str = tc.function.arguments
                            tool_args_acc[idx] = tool_args_acc.get(idx, "") + chunk_str
                            yield ToolCallDeltaEvent(
                                index=idx,
                                tool_name=tool_names.get(idx, ""),
                                args_chunk=chunk_str,
                            )

                # ── finish_reason: tool_calls → emitir ToolCallEvents completos
                finish = chunk.choices[0].finish_reason
                if finish == "tool_calls" and not tools_emitted:
                    tools_emitted = True
                    for idx, raw in tool_args_acc.items():
                        try:
                            args = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            args = {"_raw": raw}

                        yield ToolCallEvent(
                            index=idx,
                            tool_call_id=tool_ids[idx],
                            tool_name=tool_names[idx],
                            args=args,
                        )

        except APIStatusError as e:
            code = _map_error_code(e.status_code)
            yield ErrorEvent(
                message=str(e.message),
                code=code,
                retryable=code == "rate_limit",
            )
            return

        except APIConnectionError as e:
            yield ErrorEvent(
                message=str(e),
                code="connection_error",
                retryable=True,
            )
            return

        # ── Reasoning done ────────────────────────────────────────────────────
        if reasoning_acc:
            yield ReasoningDoneEvent(content=reasoning_acc)

        # ── UsageEvent ────────────────────────────────────────────────────────
        if final_usage:
            u = final_usage
            usage = TokenUsage(
                input=u.prompt_tokens,
                output=u.completion_tokens,
                # OpenAI: cached input tokens vienen en prompt_tokens_details
                cache_read=getattr(
                    getattr(u, "prompt_tokens_details", None), "cached_tokens", 0
                )
                or 0,
            )
            cost_usd, cost_known = calculate_cost(
                model=config.model,
                input_tokens=usage.input,
                output_tokens=usage.output,
                cache_read_tokens=usage.cache_read,
            )
            yield UsageEvent(
                model=config.model,
                usage=usage,
                cost_usd=cost_usd,
                cost_known=cost_known,
            )

        # ── LLMDoneEvent ──────────────────────────────────────────────────────
        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
        )


def _map_error_code(status_code: int) -> str:
    return {
        401: "auth",
        403: "permission",
        404: "not_found",
        429: "rate_limit",
        500: "server_error",
        503: "overloaded",
    }.get(status_code, "unknown")
