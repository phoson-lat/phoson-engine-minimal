import os
import json
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, APIStatusError, APIConnectionError

from phoson_llm.utils import map_error_code
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    TokenUsage,
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
from phoson_llm.chats._openai_compatible import (
    _convert_tools,
    _convert_messages,
)

# ─── Adapter ─────────────────────────────────────────────────────────────────


class OpenAIChat(BaseLLMChat):
    """Adapter for OpenAI Chat Completions API.

    Also compatible with Ollama and OpenRouter via base_url.
    Supports: streaming, tools, multimodal inputs (images, audio, video, documents).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Initialize the OpenAI client.

        Args:
            api_key: OpenAI API key. Defaults to OPENAI_API_KEY env var.
            base_url: Optional base URL for compatible APIs (Ollama, OpenRouter).
        """
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url,
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response from the OpenAI model.

        Args:
            messages: List of conversation messages.
            config: Model configuration (model, max_tokens, temperature, etc.).
            tools: Optional list of tool definitions.

        Yields:
            LLMEvent objects representing the model's response stream.
        """

        kwargs: dict = {
            "model": config.model,
            "max_completion_tokens": config.max_tokens,
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

        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

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

                reasoning_chunk = getattr(delta, "reasoning_content", None)
                if reasoning_chunk:
                    if not reasoning_acc:
                        yield ReasoningStartEvent()
                    reasoning_acc += reasoning_chunk
                    yield ReasoningTokenEvent(content=reasoning_chunk)

                if delta.tool_calls:
                    has_tool_calls = True
                    for tc in delta.tool_calls:
                        idx = tc.index

                        if tc.id:
                            tool_ids[idx] = tc.id
                            tool_names[idx] = tc.function.name or ""
                            tool_args_acc[idx] = ""

                        if tc.function and tc.function.arguments:
                            chunk_str = tc.function.arguments
                            tool_args_acc[idx] = tool_args_acc.get(idx, "") + chunk_str
                            yield ToolCallDeltaEvent(
                                index=idx,
                                tool_name=tool_names.get(idx, ""),
                                args_chunk=chunk_str,
                            )

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

                    # Clear buffers so any subsequent tool_calls in the same
                    # response (unusual but possible) start fresh.
                    tool_args_acc.clear()
                    tool_ids.clear()
                    tool_names.clear()
                    tools_emitted = False

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
            u = final_usage
            usage = TokenUsage(
                input=u.prompt_tokens,
                output=u.completion_tokens,
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
                provider="openai",
            )
            yield UsageEvent(
                model=config.model,
                usage=usage,
                cost_usd=cost_usd,
                cost_known=cost_known,
            )

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
        )
