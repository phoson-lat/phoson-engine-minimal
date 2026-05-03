import os
import json
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, APIStatusError, APIConnectionError

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


def _convert_messages(messages: list[Message]) -> list[dict]:
    """Converts Phoson messages to OpenRouter/OpenAI format."""
    result = []

    for msg in messages:
        if msg.role == "system":
            content = msg.content if isinstance(msg.content, str) else ""
            result.append({"role": "system", "content": content})
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
            continue

        text_blocks = [b for b in msg.content if isinstance(b, TextBlock)]
        tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
        tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]

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

        for b in tool_results:
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": b.tool_call_id,
                    "content": b.result,
                }
            )

        if not tool_uses and not tool_results and text_blocks:
            result.append(
                {
                    "role": msg.role,
                    "content": " ".join(b.text for b in text_blocks),
                }
            )

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Converts ToolDefinition to OpenRouter tools format."""
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


def _extract_reasoning_delta(delta: object) -> str | None:
    """Extracts reasoning_content from an OpenAI delta."""
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(delta, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_tool_args(raw: str) -> dict:
    """Safe parsing of tool arguments."""
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"command": raw} if raw.strip() else {}

    if isinstance(parsed, dict):
        return parsed

    if isinstance(parsed, str):
        return {"command": parsed}

    return {"_raw": raw}


class OpenRouterChat(BaseLLMChat):
    """Adapter for OpenRouter API (multi-provider aggregation).

    Provides unified interface to various LLM providers through OpenRouter.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        http_referer: str | None = None,
        app_title: str | None = None,
    ) -> None:
        """Initialize the OpenRouter client.

        Args:
            api_key: OpenRouter API key. Defaults to OPENROUTER_API_KEY env var.
            base_url: OpenRouter API base URL.
            http_referer: Optional HTTP referer for API calls.
            app_title: Optional application title for OpenRouter analytics.
        """
        default_headers: dict[str, str] = {}
        if http_referer:
            default_headers["HTTP-Referer"] = http_referer
        if app_title:
            default_headers["X-OpenRouter-Title"] = app_title

        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=base_url,
            default_headers=default_headers or None,
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response from the OpenRouter model.

        Args:
            messages: List of conversation messages.
            config: Model configuration (model, max_tokens, temperature, etc.).
            tools: Optional list of tool definitions.

        Yields:
            LLMEvent objects representing the model's response stream.
        """
        kwargs: dict = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "messages": _convert_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if config.system:
            kwargs["messages"] = [
                m for m in kwargs["messages"] if m.get("role") != "system"
            ]
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

        if config.reasoning_effort:
            kwargs["reasoning_effort"] = config.reasoning_effort
            kwargs.pop("temperature", None)

        text_acc = ""
        reasoning_acc = ""
        tool_args_acc: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_ids: dict[int, str] = {}
        has_tool_calls = False
        final_usage = None
        tools_emitted = False

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        try:
            async for chunk in await self._client.chat.completions.create(**kwargs):
                if not chunk.choices:
                    if chunk.usage:
                        final_usage = chunk.usage
                    continue

                delta = chunk.choices[0].delta

                if delta.content:
                    text_acc += delta.content
                    yield TokenEvent(content=delta.content)

                reasoning_chunk = _extract_reasoning_delta(delta)
                if reasoning_chunk:
                    if not reasoning_acc:
                        yield ReasoningStartEvent()
                    reasoning_acc += reasoning_chunk
                    yield ReasoningTokenEvent(content=reasoning_chunk)

                if delta.tool_calls:
                    has_tool_calls = True
                    for tc in delta.tool_calls:
                        idx = tc.index

                        tool_args_acc.setdefault(idx, "")

                        if tc.id:
                            tool_ids[idx] = tc.id

                        if tc.function and tc.function.name:
                            tool_names[idx] = tc.function.name

                        if tc.function and tc.function.arguments:
                            chunk_str = tc.function.arguments
                            tool_args_acc[idx] += chunk_str
                            yield ToolCallDeltaEvent(
                                index=idx,
                                tool_name=tool_names.get(idx, ""),
                                args_chunk=chunk_str,
                            )

                finish = chunk.choices[0].finish_reason
                if finish == "tool_calls" and not tools_emitted:
                    tools_emitted = True
                    for idx, raw in tool_args_acc.items():
                        if idx not in tool_ids or idx not in tool_names:
                            continue

                        yield ToolCallEvent(
                            index=idx,
                            tool_call_id=tool_ids[idx],
                            tool_name=tool_names[idx],
                            args=_parse_tool_args(raw),
                        )

        except APIStatusError as e:
            code = map_error_code(e.status_code)
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

        if reasoning_acc:
            yield ReasoningDoneEvent(content=reasoning_acc)

        if final_usage:
            usage = TokenUsage(
                input=getattr(final_usage, "prompt_tokens", 0) or 0,
                output=getattr(final_usage, "completion_tokens", 0) or 0,
                cache_read=getattr(
                    getattr(final_usage, "prompt_tokens_details", None),
                    "cached_tokens",
                    0,
                )
                or 0,
            )
            yield UsageEvent(
                model=config.model,
                usage=usage,
                cost_usd=0.0,
                cost_known=False,
            )

        yield LLMDoneEvent(content=text_acc, has_tool_calls=has_tool_calls)
