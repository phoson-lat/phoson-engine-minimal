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
    """Converts Phoson's internal format to the format expected by Ollama.

    The Ollama ``/api/chat`` endpoint expects the ``system`` prompt as a
    regular message with ``role="system"``, **not** as a top-level field, so
    we keep system messages here.

    Args:
        messages: List of Phoson messages.

    Returns:
        List of formatted messages for Ollama.
    """
    result: list[dict] = []

    for msg in messages:
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


def _prepend_system(messages: list[dict], system: str) -> list[dict]:
    """Prepend a system message to the converted message list.

    If a system message is already present at the start, it is replaced.
    """
    if messages and messages[0].get("role") == "system":
        return [{"role": "system", "content": system}, *messages[1:]]
    return [{"role": "system", "content": system}, *messages]


# ─── Adapter ─────────────────────────────────────────────────────────────────


class OllamaChat(BaseLLMChat):
    """Adapter for Ollama local LLM inference API (``/api/chat``).

    Supports running Llama, Mistral, and other models locally.

    Reference: https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-chat-completion
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
        converted = _convert_messages(messages)
        system = config.system or _extract_system_text(messages)
        if system:
            converted = _prepend_system(converted, system)

        payload: dict = {
            "model": config.model,
            "messages": converted,
            "stream": True,
        }

        options: dict = {}
        if config.temperature is not None:
            options["temperature"] = config.temperature
        if config.max_tokens:
            # Ollama's `num_predict` controls max output tokens; -1 means
            # "until EOS". We always forward the configured value because the
            # caller already chose it deliberately.
            options["num_predict"] = config.max_tokens
        if options:
            payload["options"] = options

        if tools:
            payload["tools"] = _convert_tools(tools)

        text_acc = ""
        tool_args_acc: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_ids: dict[int, str] = {}
        has_tool_calls = False
        final_usage: dict | None = None

        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

        url = f"{self._base_url}/api/chat"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        # Drain the body so the error message is helpful.
                        body = b""
                        try:
                            async for chunk in response.aiter_bytes():
                                body += chunk
                                if len(body) > 4096:
                                    break
                        except Exception:  # noqa: BLE001
                            pass
                        detail = body.decode("utf-8", errors="replace").strip()
                        msg = f"Ollama API error {response.status_code}"
                        if detail:
                            msg = f"{msg}: {detail}"
                        yield ErrorEvent(
                            message=msg,
                            code=map_error_code(response.status_code),
                            retryable=_is_retryable_status(response.status_code),
                        )
                        return

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue

                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Ollama streams one JSON object per line. While the
                        # response is in progress, ``done`` is false and
                        # ``message`` carries an incremental delta. The final
                        # line has ``done: true`` plus usage stats at the
                        # top level (``eval_count``, ``prompt_eval_count``,
                        # etc.) and an optional empty ``message``.
                        msg = data.get("message") or {}

                        content = msg.get("content", "")
                        if content:
                            text_acc += content
                            yield TokenEvent(content=content)

                        tool_calls = msg.get("tool_calls", [])
                        if tool_calls:
                            has_tool_calls = True
                            for tc_idx, tc in enumerate(tool_calls):
                                idx = tc.get("index", tc_idx)
                                func = tc.get("function", {}) or {}
                                name = func.get("name", "")

                                if "id" in tc or idx not in tool_ids:
                                    tool_ids[idx] = tc.get("id", f"tool_{idx}")
                                    tool_names[idx] = name or tool_names.get(
                                        idx, ""
                                    )
                                    tool_args_acc.setdefault(idx, "")

                                if name and not tool_names.get(idx):
                                    tool_names[idx] = name

                                func_args = func.get("arguments", "")
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

                        if data.get("done"):
                            # Final stats live at the top level of the last
                            # message. ``eval_count`` is output tokens,
                            # ``prompt_eval_count`` is input tokens.
                            if "eval_count" in data or "prompt_eval_count" in data:
                                final_usage = {
                                    "output": int(data.get("eval_count", 0) or 0),
                                    "input": int(
                                        data.get("prompt_eval_count", 0) or 0
                                    ),
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
                retryable=_is_retryable_status(e.response.status_code),
            )
            return

        if has_tool_calls and tool_args_acc:
            for idx, raw in tool_args_acc.items():
                try:
                    args = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    import warnings
                    warnings.warn(
                        f"Could not parse tool args JSON from Ollama stream "
                        f"(tool={tool_names.get(idx)!r}); stored as _raw.",
                        UserWarning,
                        stacklevel=2,
                    )
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


def _extract_system_text(messages: list[Message]) -> str | None:
    """Extract the first system message's text content, if any."""
    for msg in messages:
        if msg.role == "system" and isinstance(msg.content, str):
            return msg.content
    return None


def _is_retryable_status(status: int) -> bool:
    """Return True for HTTP statuses that warrant a retry."""
    return status == 429 or 500 <= status < 600
