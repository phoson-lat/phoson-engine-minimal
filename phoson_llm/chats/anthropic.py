import os
import json
from collections.abc import AsyncIterator

import anthropic

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

# ─── Conversión de mensajes Phoson → Anthropic ────────────────────────────────


def _convert_messages(messages: list[Message]) -> list[dict]:
    """
    Convierte el formato interno de Phoson al formato que espera Anthropic.

    Reglas:
    - role=system se separa y se pasa como parámetro `system` (manejado en stream())
    - ToolUseBlock  → {"type": "tool_use", ...}   en role=assistant
    - ToolResultBlock → {"type": "tool_result", ...} envuelto en role=user
    """
    result = []

    for msg in messages:
        if msg.role == "system":
            continue  # se pasa aparte como parámetro `system`

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
            continue

        # content es lista de ContentBlocks
        blocks = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})

            elif isinstance(block, ToolUseBlock):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.tool_call_id,
                        "name": block.tool_name,
                        "input": block.args,
                    }
                )

            elif isinstance(block, ToolResultBlock):
                # Anthropic espera tool_result dentro de un mensaje role=user
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_call_id,
                        "content": block.result,
                        "is_error": block.error,
                    }
                )

        if blocks:
            result.append({"role": msg.role, "content": blocks})

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Convierte ToolDefinition al formato de tools de Anthropic."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]


def _extract_system(messages: list[Message]) -> str | None:
    """Extrae el primer mensaje system de la lista."""
    for msg in messages:
        if msg.role == "system":
            return msg.content if isinstance(msg.content, str) else None
    return None


# ─── Adapter ─────────────────────────────────────────────────────────────────


class AnthropicChat(BaseLLMChat):
    """
    Adapter para Anthropic Claude.
    Soporta: streaming, extended thinking, tool use, prompt caching.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
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
        }

        system = config.system or _extract_system(messages)
        if system:
            kwargs["system"] = system

        if config.temperature is not None:
            kwargs["temperature"] = config.temperature

        if tools:
            kwargs["tools"] = _convert_tools(tools)

        # Extended thinking: requiere deshabilitar temperature
        if config.thinking_budget:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": config.thinking_budget,
            }
            kwargs.pop("temperature", None)  # incompatible con thinking

        # ── Estado interno del stream ─────────────────────────────────────────
        text_acc = ""  # texto final acumulado
        reasoning_acc = ""  # reasoning acumulado
        tool_args_acc: dict[int, str] = {}  # index → partial JSON acumulado
        tool_names: dict[int, str] = {}  # index → nombre de la tool
        tool_ids: dict[int, str] = {}  # index → tool_call_id
        has_tool_calls = False

        # ── Emitir LLMStartEvent ──────────────────────────────────────────────
        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

        # ── Stream ────────────────────────────────────────────────────────────
        try:
            async with self._client.messages.stream(**kwargs) as s:
                async for event in s:
                    etype = event.type

                    # ── Texto ─────────────────────────────────────────────────
                    if etype == "content_block_delta":
                        delta = event.delta  # type: ignore

                        if delta.type == "text_delta":  # type: ignore
                            text_acc += delta.text  # type: ignore
                            yield TokenEvent(content=delta.text)  # type: ignore

                        # ── Reasoning (thinking) ──────────────────────────────
                        elif delta.type == "thinking_delta":  # type: ignore
                            if not reasoning_acc:
                                # primer chunk → emitir start
                                yield ReasoningStartEvent()
                            reasoning_acc += delta.thinking  # type: ignore
                            yield ReasoningTokenEvent(content=delta.thinking)

                        # ── Tool call args (partial JSON) ─────────────────────
                        elif delta.type == "input_json_delta":  # type: ignore
                            idx = event.index  # type: ignore
                            tool_args_acc[idx] = (
                                tool_args_acc.get(idx, "") + delta.partial_json
                            )  # type: ignore
                            yield ToolCallDeltaEvent(
                                index=idx,
                                tool_name=tool_names.get(idx, ""),
                                args_chunk=delta.partial_json,  # type: ignore
                            )

                    # ── Inicio de bloque de contenido ─────────────────────────
                    elif etype == "content_block_start":
                        block = event.content_block  # type: ignore
                        idx = event.index  # type: ignore

                        if block.type == "tool_use":
                            has_tool_calls = True
                            tool_names[idx] = block.name
                            tool_ids[idx] = block.id
                            tool_args_acc[idx] = ""

                    # ── Fin de bloque de contenido ────────────────────────────
                    elif etype == "content_block_stop":
                        idx = event.index  # type: ignore

                        # Reasoning completo
                        if reasoning_acc and idx == 0:
                            yield ReasoningDoneEvent(content=reasoning_acc)

                        # Tool call completa — emitir ToolCallEvent con args parseados
                        if idx in tool_args_acc and tool_names.get(idx):
                            raw = tool_args_acc[idx]
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

                    # ── Usage (message_delta lleva el usage final) ────────────
                    elif etype == "message_delta":
                        if hasattr(event, "usage") and event.usage:  # type: ignore
                            u = event.usage  # type: ignore
                            # input_tokens viene en message_start,
                            # output en message_delta
                            # los acumulamos via get_final_message al terminar
                            pass

                # ── Fuera del loop: obtener usage final acumulado ─────────────
                final_msg = await s.get_final_message()
                u = final_msg.usage

                usage = TokenUsage(
                    input=u.input_tokens,
                    output=u.output_tokens,
                    cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
                    cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                )
                cost_usd, cost_known = calculate_cost(
                    model=config.model,
                    input_tokens=usage.input,
                    output_tokens=usage.output,
                    cache_write_tokens=usage.cache_write,
                    cache_read_tokens=usage.cache_read,
                    provider="anthropic",
                )
                yield UsageEvent(
                    model=config.model,
                    usage=usage,
                    cost_usd=cost_usd,
                    cost_known=cost_known,
                )

        except anthropic.APIStatusError as e:
            code = _map_error_code(e.status_code)
            yield ErrorEvent(
                message=str(e.message),
                code=code,
                retryable=code in ("rate_limit", "overloaded"),
            )
            return

        except anthropic.APIConnectionError as e:
            yield ErrorEvent(
                message=str(e),
                code="connection_error",
                retryable=True,
            )
            return

        # ── LLMDoneEvent final ────────────────────────────────────────────────
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
        529: "overloaded",
    }.get(status_code, "unknown")
