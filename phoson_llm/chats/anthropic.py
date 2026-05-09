import os
import json
from typing import cast
from collections.abc import AsyncIterator

import anthropic
from anthropic.types import (
    TextDelta,
    ThinkingDelta,
    InputJSONDelta,
)

from phoson_llm.utils import guess_mime, map_error_code, load_file_as_base64
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
    DocumentBlock,
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
    Converts a multimodal ContentBlock to the format expected by Anthropic.

    Args:
        block (ContentBlock): The content block to convert.

    Returns:
        dict: Formatted dictionary for the Anthropic API.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ImageBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path).split(",", 1)[-1]
            media = block.media_type or guess_mime(path)
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": data,
                },
            }
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": source,
            },
        }

    if isinstance(block, DocumentBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path).split(",", 1)[-1]
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data,
                },
            }
        return {
            "type": "document",
            "source": {
                "type": "url",
                "url": source,
            },
        }

    if isinstance(block, AudioBlock):
        return {
            "type": "text",
            "text": f"[Audio not supported by Anthropic: {block.source}]",
        }

    if isinstance(block, VideoBlock):
        return {
            "type": "text",
            "text": f"[Video not supported by Anthropic: {block.source}]",
        }

    if isinstance(block, (ToolUseBlock, ToolResultBlock)):
        raise TypeError(
            f"ToolUseBlock/ToolResultBlock should not reach _convert_content_block. "
            f"Got: {type(block)}"
        )

    return {"type": "text", "text": f"[Unsupported block: {type(block).__name__}]"}


def _convert_messages(messages: list[Message]) -> list[dict]:
    """
    Converts Phoson's internal format to the format expected by Anthropic.

    Args:
        messages (list[Message]): List of Phoson messages.

    Returns:
        list[dict]: List of formatted messages for the Anthropic API.
    """
    result = []

    for msg in messages:
        if msg.role == "system":
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
            continue

        blocks = []
        tool_uses = []
        tool_results = []
        multimodal_blocks = []

        for block in msg.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                tool_uses.append(block)
            elif isinstance(block, ToolResultBlock):
                tool_results.append(block)
            else:
                multimodal_blocks.append(block)

        for b in tool_uses:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": b.tool_call_id,
                    "name": b.tool_name,
                    "input": b.args,
                }
            )

        for b in tool_results:
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "content": b.result,
                    "is_error": b.error,
                }
            )

        for b in multimodal_blocks:
            blocks.append(_convert_content_block(b))

        if blocks:
            result.append({"role": msg.role, "content": blocks})

    return result


def _convert_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Converts ToolDefinition to Anthropic's tools format."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
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


class AnthropicChat(BaseLLMChat):
    """Adapter for Anthropic Claude API.

    Supports: streaming, extended thinking, tool use, prompt caching, multimodal inputs.
    """

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize the Anthropic client.

        Args:
            api_key: Anthropic API key. Defaults to ANTHROPIC_API_KEY env var.
        """
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response from the Anthropic model.

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
        }

        system = config.system or _extract_system(messages)
        if system:
            kwargs["system"] = system

        if config.temperature is not None:
            kwargs["temperature"] = config.temperature

        if tools:
            kwargs["tools"] = _convert_tools(tools)

        if config.thinking_budget:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": config.thinking_budget,
            }
            kwargs.pop("temperature", None)

        text_acc = ""
        reasoning_acc = ""
        tool_args_acc: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_ids: dict[int, str] = {}
        has_tool_calls = False

        yield LLMStartEvent(
            model=config.model,
            message_count=len(messages),
        )

        try:
            async with self._client.messages.stream(**kwargs) as s:
                async for event in s:
                    # We dispatch on event.type (the discriminator field of
                    # the RawMessageStreamEvent union) and cast the relevant
                    # payloads. This keeps the code resilient to SDK
                    # additions of new event variants.
                    etype = event.type

                    if etype == "content_block_delta":
                        # Narrow to RawContentBlockDeltaEvent payload shape.
                        delta = event.delta  # type: ignore[union-attr]
                        idx = event.index  # type: ignore[union-attr]
                        dtype = delta.type

                        if dtype == "text_delta":
                            text_delta = cast(TextDelta, delta)
                            text_acc += text_delta.text
                            yield TokenEvent(content=text_delta.text)

                        elif dtype == "thinking_delta":
                            thinking_delta = cast(ThinkingDelta, delta)
                            if not reasoning_acc:
                                yield ReasoningStartEvent()
                            reasoning_acc += thinking_delta.thinking
                            yield ReasoningTokenEvent(content=thinking_delta.thinking)

                        elif dtype == "input_json_delta":
                            json_delta = cast(InputJSONDelta, delta)
                            tool_args_acc[idx] = (
                                tool_args_acc.get(idx, "") + json_delta.partial_json
                            )
                            yield ToolCallDeltaEvent(
                                index=idx,
                                tool_name=tool_names.get(idx, ""),
                                args_chunk=json_delta.partial_json,
                            )

                    elif etype == "content_block_start":
                        # Narrow to RawContentBlockStartEvent payload shape.
                        block = event.content_block  # type: ignore[union-attr]
                        idx = event.index  # type: ignore[union-attr]

                        if block.type == "tool_use":
                            has_tool_calls = True
                            tool_names[idx] = block.name
                            tool_ids[idx] = block.id
                            tool_args_acc[idx] = ""

                    elif etype == "content_block_stop":
                        idx = event.index  # type: ignore[union-attr]

                        if reasoning_acc and idx == 0:
                            yield ReasoningDoneEvent(content=reasoning_acc)

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
            code = map_error_code(e.status_code)
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

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
        )
