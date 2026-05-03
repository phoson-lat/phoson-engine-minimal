import os
import json
import base64
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, APIStatusError, APIConnectionError

from phoson_llm.utils import map_error_code, load_file_as_base64
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    AudioBlock,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    ContentBlock,
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

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _convert_content_block(block: ContentBlock) -> dict:
    """
    Converts a Phoson ContentBlock to the format expected by OpenAI.

    Args:
        block (ContentBlock): The content block to convert.

    Returns:
        dict: Formatted dictionary for the OpenAI API.

    Raises:
        TypeError: If an unsupported block is passed.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ImageBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            source = load_file_as_base64(path, block.media_type)
        return {
            "type": "image_url",
            "image_url": {
                "url": source,
                "detail": block.detail,
            },
        }

    if isinstance(block, AudioBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        else:
            b64 = source
        return {
            "type": "input_audio",
            "input_audio": {
                "data": b64,
                "format": block.format,
            },
        }

    if isinstance(block, VideoBlock):
        return {
            "type": "text",
            "text": f"[Video not directly supported by OpenAI: {block.source}]",
        }

    if isinstance(block, (ToolUseBlock, ToolResultBlock)):
        raise TypeError(
            f"ToolUseBlock/ToolResultBlock should not reach _convert_content_block. "
            f"Got: {type(block)}"
        )

    return {
        "type": "text",
        "text": f"[Unsupported content block: {type(block).__name__}]",
    }


def _convert_messages(messages: list[Message]) -> list[dict]:
    """
    Converts Phoson's internal format to the format expected by OpenAI.

    Args:
        messages (list[Message]): List of Phoson messages.

    Returns:
        list[dict]: List of formatted messages for the OpenAI API.
    """
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
        multimodal_blocks = [
            b
            for b in msg.content
            if not isinstance(b, (TextBlock, ToolUseBlock, ToolResultBlock))
        ]

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

        if multimodal_blocks:
            parts = [_convert_content_block(b) for b in multimodal_blocks]
            if text_blocks:
                parts.insert(
                    0, {"type": "text", "text": " ".join(b.text for b in text_blocks)}
                )
            result.append({"role": msg.role, "content": parts})

        elif text_blocks and not tool_uses and not tool_results:
            result.append(
                {
                    "role": msg.role,
                    "content": " ".join(b.text for b in text_blocks),
                }
            )

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Converts ToolDefinition to OpenAI's tools format."""
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
