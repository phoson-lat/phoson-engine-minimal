import json
from collections.abc import AsyncIterator

import httpx

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
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolCallDeltaEvent,
)
from phoson_llm.chats.base import BaseLLMChat

# ─── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 300.0  # 5 minutos para modelos grandes


# ─── Conversión de mensajes Phoson → Ollama ─────────────────────────────────


def _convert_messages(messages: list[Message]) -> list[dict]:
    """
    Convierte el formato interno de Phoson al formato que espera Ollama.

    Diferencias clave vs OpenAI:
    - No hay role="system" - se pasa como parámetro `system` en el request
    - Solo acepta content como string (no array de blocks)
    - tool_calls y tool_results funcionan diferente
    """
    result = []

    for msg in messages:
        if msg.role == "system":
            continue  # system se maneja por separado

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
            continue

        # content es lista de ContentBlocks - extraer texto
        text_parts = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            # Ollama no soporta tool_use/tool_result en formato de mensaje
            # Se manejan vía tool_calls en el response

        if text_parts:
            result.append({"role": msg.role, "content": " ".join(text_parts)})

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """
    Convierte ToolDefinition al formato de tools de Ollama.

    Ollama soporta tools desde la versión 0.1.20+.
    Formato: array de objetos con name, description y parameters.
    """
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


def _extract_system(messages: list[Message]) -> str | None:
    """Extrae el primer mensaje system de la lista."""
    for msg in messages:
        if msg.role == "system":
            return msg.content if isinstance(msg.content, str) else None
    return None


# ─── Adapter ─────────────────────────────────────────────────────────────────


class OllamaChat(BaseLLMChat):
    """
    Adapter para Ollama API (/api/chat).

    Soporta:
    - Streaming de texto
    - Tool use (desde Ollama 0.1.20+)
    - Modelos con reasoning (e.g. deepseek-r1)

    Ejemplo:
        OllamaChat()
        OllamaChat(base_url="http://192.168.1.100:11434")
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:

        # ── Construir payload del request ─────────────────────────────────────
        payload: dict = {
            "model": config.model,
            "messages": _convert_messages(messages),
            "stream": True,
        }

        # System: desde config o extraer de mensajes
        system = config.system or _extract_system(messages)
        if system:
            payload["system"] = system

        if config.temperature is not None:
            payload["temperature"] = config.temperature

        if config.max_tokens and config.max_tokens != 4096:
            payload["options"] = payload.get("options", {})
            payload["options"]["num_predict"] = config.max_tokens

        # Tools - solo si hay definidos
        if tools:
            payload["tools"] = _convert_tools(tools)

        # ── Estado interno del stream ─────────────────────────────────────────
        text_acc = ""
        tool_args_acc: dict[int, str] = {}  # index → partial JSON acumulado
        tool_names: dict[int, str] = {}  # index → nombre
        tool_ids: dict[int, str] = {}  # index → tool_call_id
        has_tool_calls = False
        final_usage = None

        # ── LLMStartEvent ─────────────────────────────────────────────────────
        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

        # ── Stream ────────────────────────────────────────────────────────────
        url = f"{self._base_url}/api/chat"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        yield ErrorEvent(
                            message=f"Ollama API error: {response.status_code}",
                            code=_map_status_code(response.status_code),
                            retryable=response.status_code == 429,
                        )
                        return

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue

                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # ── Message delta ( contenido de respuesta ) ───────────
                        msg = data.get("message", {})
                        msg_type = msg.get("type", "message")

                        if msg_type == "message":
                            # ── Texto ─────────────────────────────────────────
                            content = msg.get("content", "")
                            if content:
                                text_acc += content
                                yield TokenEvent(content=content)

                            # ── Tool calls ────────────────────────────────────
                            tool_calls = msg.get("tool_calls", [])
                            if tool_calls:
                                has_tool_calls = True
                                for tc in tool_calls:
                                    idx = tc.get("index", 0)

                                    # Inicio de tool call
                                    if "id" in tc:
                                        tool_ids[idx] = tc["id"]
                                        tool_names[idx] = tc.get("function", {}).get(
                                            "name", ""
                                        )
                                        tool_args_acc[idx] = ""

                                    # Args (puede venir en múltiples chunks)
                                    func_args = tc.get("function", {}).get(
                                        "arguments", ""
                                    )
                                    if func_args:
                                        if isinstance(func_args, str):
                                            chunk_str = func_args
                                        else:
                                            chunk_str = json.dumps(func_args)

                                        tool_args_acc[idx] = (
                                            tool_args_acc.get(idx, "") + chunk_str
                                        )
                                        yield ToolCallDeltaEvent(
                                            index=idx,
                                            tool_name=tool_names.get(idx, ""),
                                            args_chunk=chunk_str,
                                        )

                        # ── Done signal ────────────────────────────────────────
                        elif msg_type == "done":
                            # Ollama puede enviar stats de usage al final
                            # Algunos modelos envían eval_count, prompt_count
                            if "eval_count" in data:
                                final_usage = {
                                    "output": data.get("eval_count", 0),
                                    "input": data.get("prompt_eval_count", 0),
                                }

        except httpx.ConnectError as e:
            yield ErrorEvent(
                message=f"Cannot connect to Ollama at {self._base_url}: {e}",
                code="connection_error",
                retryable=True,
            )
            return

        except httpx.TimeoutException as e:
            yield ErrorEvent(
                message=f"Request timed out: {e}",
                code="timeout",
                retryable=True,
            )
            return

        except httpx.HTTPStatusError as e:
            yield ErrorEvent(
                message=str(e),
                code=_map_status_code(e.response.status_code),
                retryable=e.response.status_code == 429,
            )
            return

        # ── Reasoning done ───────────────────────────────────────────────────
        # Ollama modelos como deepseek-r1 envían reasoning en content
        # con formato específico (puede variar según modelo)
        # Por ahora no emitimos ReasoningDoneEvent automáticamente
        # ya que Ollama no tiene un campo separado para reasoning

        # ── Tool calls completos ──────────────────────────────────────────────
        if has_tool_calls and tool_args_acc:
            for idx, raw in tool_args_acc.items():
                try:
                    args = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    args = {"_raw": raw}

                yield ToolCallEvent(
                    index=idx,
                    tool_call_id=tool_ids.get(idx, f"tool_{idx}"),
                    tool_name=tool_names.get(idx, ""),
                    args=args,
                )

        # ── UsageEvent ────────────────────────────────────────────────────────
        if final_usage:
            usage = TokenUsage(
                input=final_usage.get("input", 0),
                output=final_usage.get("output", 0),
            )
            # Ollama no tiene pricing - cost_known=False
            yield UsageEvent(
                model=config.model,
                usage=usage,
                cost_usd=0.0,
                cost_known=False,
            )

        # ── LLMDoneEvent ──────────────────────────────────────────────────────
        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
        )


def _map_status_code(status_code: int) -> str:
    return {
        401: "auth",
        403: "permission",
        404: "not_found",
        429: "rate_limit",
        500: "server_error",
        503: "overloaded",
    }.get(status_code, "unknown")
