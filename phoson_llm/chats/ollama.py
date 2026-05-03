import json
from collections.abc import AsyncIterator

import httpx

from phoson_llm.utils import map_error_code
from phoson_llm.schemas import (
    Message,
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

# ─── Constants ───────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 300.0


# ─── Message conversion Phoson → Ollama ─────────────────────────────────


def _convert_messages(messages: list[Message]) -> list[dict]:
    """
    Convierte el formato interno de Phoson al formato que espera Ollama.

    Args:
        messages (list[Message]): Lista de mensajes de Phoson.

    Returns:
        list[dict]: Lista de mensajes formateados para Ollama.
    """
    result = []

    for msg in messages:
        if msg.role == "system":
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
            continue

        text_parts = [
            block.text for block in msg.content if isinstance(block, TextBlock)
        ]

        if text_parts:
            result.append({"role": msg.role, "content": " ".join(text_parts)})

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Converts ToolDefinition to Ollama tools format."""
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
    """Extracts the first system message from the list."""
    for msg in messages:
        if msg.role == "system":
            return msg.content if isinstance(msg.content, str) else None
    return None


# ─── Adapter ─────────────────────────────────────────────────────────────────


class OllamaChat(BaseLLMChat):
    """Adapter for Ollama local LLM inference API (/api/chat).

    Supports running Llama, Mistral, and other models locally.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the Ollama client.

        Args:
            base_url: Ollama API base URL (default: http://localhost:11434).
            timeout: Request timeout in seconds.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response from the Ollama model.

        Args:
            messages: List of conversation messages.
            config: Model configuration (model, max_tokens, temperature, etc.).
            tools: Optional list of tool definitions.

        Yields:
            LLMEvent objects representing the model's response stream.
        """
        payload: dict = {
            "model": config.model,
            "messages": _convert_messages(messages),
            "stream": True,
        }

        system = config.system or _extract_system(messages)
        if system:
            payload["system"] = system

        if config.temperature is not None:
            payload["temperature"] = config.temperature

        if config.max_tokens and config.max_tokens != 4096:
            payload["options"] = payload.get("options", {})
            payload["options"]["num_predict"] = config.max_tokens

        if tools:
            payload["tools"] = _convert_tools(tools)

        text_acc = ""
        tool_args_acc: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_ids: dict[int, str] = {}
        has_tool_calls = False
        final_usage = None

        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

        url = f"{self._base_url}/api/chat"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        yield ErrorEvent(
                            message=f"Ollama API error: {response.status_code}",
                            code=map_error_code(response.status_code),
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

                        msg = data.get("message", {})
                        msg_type = msg.get("type", "message")

                        if msg_type == "message":
                            content = msg.get("content", "")
                            if content:
                                text_acc += content
                                yield TokenEvent(content=content)

                            tool_calls = msg.get("tool_calls", [])
                            if tool_calls:
                                has_tool_calls = True
                                for tc in tool_calls:
                                    idx = tc.get("index", 0)

                                    if "id" in tc:
                                        tool_ids[idx] = tc["id"]
                                        tool_names[idx] = tc.get("function", {}).get(
                                            "name", ""
                                        )
                                        tool_args_acc[idx] = ""

                                    func_args = tc.get("function", {}).get(
                                        "arguments", ""
                                    )
                                    if func_args:
                                        chunk_str = (
                                            func_args
                                            if isinstance(func_args, str)
                                            else json.dumps(func_args)
                                        )
                                        tool_args_acc[idx] = (
                                            tool_args_acc.get(idx, "") + chunk_str
                                        )
                                        yield ToolCallDeltaEvent(
                                            index=idx,
                                            tool_name=tool_names.get(idx, ""),
                                            args_chunk=chunk_str,
                                        )

                        elif msg_type == "done":
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
                code=map_error_code(e.response.status_code),
                retryable=e.response.status_code == 429,
            )
            return

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

        if final_usage:
            usage = TokenUsage(
                input=final_usage.get("input", 0),
                output=final_usage.get("output", 0),
            )
            yield UsageEvent(
                model=config.model,
                usage=usage,
                cost_usd=0.0,
                cost_known=False,
            )

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
        )
