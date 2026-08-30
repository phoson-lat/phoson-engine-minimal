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

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    guess_mime,
    map_error_code,
    load_file_as_base64,
    is_context_length_error,
    missing_attachment_placeholder,
)
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    AudioBlock,
    ErrorEvent,
    ImageBlock,
    JsonObject,
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


# Prompt-caching breakpoint marker (Anthropic explicit caching). At most
# four blocks may carry it per request; the adapter reserves them as
# follows (see ``stream``): the stable system prompt, the tool list
# (when present) and the last cacheable block of the last message —
# which advances the cached prefix as the conversation grows.
_EPHEMERAL: JsonObject = {"type": "ephemeral"}


def _convert_content_block(block: ContentBlock) -> JsonObject:
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
            data = load_file_as_base64(path)
            if data is None:
                return {
                    "type": "text",
                    "text": missing_attachment_placeholder("image", path),
                }
            data = data.split(",", 1)[-1]
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
            data = load_file_as_base64(path)
            if data is None:
                return {
                    "type": "text",
                    "text": missing_attachment_placeholder("document", path),
                }
            data = data.split(",", 1)[-1]
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


def _convert_messages(
    messages: list[Message], cache_last: bool = False
) -> list[JsonObject]:
    """
    Converts Phoson's internal format to the format expected by Anthropic.

    Args:
        messages (list[Message]): List of Phoson messages.
        cache_last: When True, the last cacheable block of the last
            message is tagged with an ephemeral prompt-caching
            breakpoint, so the cached prefix extends across turns as the
            conversation grows. ``tool_use`` blocks are skipped — the API
            does not accept a ``cache_control`` marker on them.

    Returns:
        list[dict]: List of formatted messages for the Anthropic API.
    """
    result = []

    for msg_index, msg in enumerate(messages):
        if msg.role == "system":
            continue

        if isinstance(msg.content, str):
            if cache_last and msg_index == len(messages) - 1:
                result.append(
                    {
                        "role": msg.role,
                        "content": [
                            {
                                "type": "text",
                                "text": msg.content,
                                "cache_control": _EPHEMERAL,
                            },
                        ],
                    }
                )
            else:
                result.append({"role": msg.role, "content": msg.content})
            continue

        is_last = msg_index == len(messages) - 1
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

        if cache_last and is_last:
            # Anchor the cache on the last block of the last message,
            # except ``tool_use``: the API rejects a ``cache_control``
            # marker on assistant tool-use blocks (it is accepted on
            # text, image, document and tool_result blocks). In a ReAct
            # loop the last message is usually a user turn carrying
            # tool results, so anchoring there keeps the entire history
            # — tool calls and results included — inside the cached
            # prefix for the next turn.
            for b in reversed(blocks):
                if b.get("type") != "tool_use" and "cache_control" not in b:
                    b["cache_control"] = _EPHEMERAL
                    break

        if blocks:
            result.append({"role": msg.role, "content": blocks})

    return result


def _convert_tools(
    tools: list[ToolDefinition], cache_last: bool = False
) -> list[JsonObject]:
    """Converts ToolDefinition to Anthropic's tools format.

    When ``cache_last`` is set, the final tool definition is tagged with
    an ephemeral prompt-caching breakpoint: the tool list is part of the
    stable prefix, so caching through it saves the whole list on every
    subsequent request of the session.
    """
    converted = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]
    if cache_last and converted:
        converted[-1]["cache_control"] = _EPHEMERAL
    return converted


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

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Initialize the Anthropic client.

        Args:
            api_key: Anthropic API key. Defaults to ANTHROPIC_API_KEY env var.
            base_url: Optional override for the API base URL (proxies, etc.).
        """
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            base_url=base_url,
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.close()

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

        # Prompt caching (IMPROVEMENTS.md G2 / #69): the request is built
        # so the entire stable prefix — system prompt, tool list, and
        # everything up to the end of the conversation history — is
        # covered by ephemeral cache breakpoints. Three of the four
        # allowed breakpoints are used; the last-message one advances as
        # the conversation grows, so each turn re-reads the whole prior
        # history from cache instead of re-billing it as fresh input.
        # The system prompt must stay a stable prefix (date, not live
        # clock — see phoson_cli.session_utils).
        kwargs: dict = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "messages": _convert_messages(messages, cache_last=True),
        }

        system = config.system or _extract_system(messages)
        if system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": _EPHEMERAL},
            ]

        if config.temperature is not None:
            kwargs["temperature"] = config.temperature

        if tools:
            kwargs["tools"] = _convert_tools(tools, cache_last=True)

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
                        # ``Delta`` in the SDK is a union of several payload
                        # classes; ``getattr`` keeps the dispatch open without
                        # requiring a static `.type` on every member.
                        dtype = getattr(delta, "type", None)

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
                                args: JsonObject = json.loads(raw) if raw else {}
                            except json.JSONDecodeError:
                                import warnings

                                warnings.warn(
                                    "Could not parse tool args JSON from Anthropic"
                                    f" stream (tool={tool_names.get(idx)!r});"
                                    " stored as _raw.",
                                    UserWarning,
                                    stacklevel=2,
                                )
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
            if is_context_length_error(e.status_code, str(e.message)):
                code = CONTEXT_LENGTH_ERROR_CODE
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
