"""Google Gemini adapter using the native SDK."""

import os
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator

from phoson_llm.utils import load_file_as_base64
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    AudioBlock,
    ImageBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    DocumentBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
)
from phoson_llm.chats.base import BaseLLMChat

if TYPE_CHECKING:
    from google import genai
    from google.genai import types


def _convert_messages(messages: list[Message]) -> "list[types.Content]":
    """Converts Phoson messages to Gemini Content objects."""
    from google.genai import types

    gemini_messages = []
    for msg in messages:
        if msg.role == "system":
            continue

        parts = []
        if isinstance(msg.content, str):
            parts.append(types.Part.from_text(text=msg.content))
        else:
            for block in msg.content:
                part_or_text = _convert_block(types, block)
                parts.append(part_or_text)

        role = "user" if msg.role == "user" else "model"
        gemini_messages.append(types.Content(role=role, parts=parts))

    return gemini_messages


def _convert_block(types: Any, block: Any) -> Any:
    """Convert a single Phoson ContentBlock to a Gemini Part.

    Local ``file://`` sources are read and base64-encoded inline
    (Gemini's ``file_uri`` only accepts Google-hosted resources, never
    local paths). Unsupported block types become a visible text
    placeholder instead of being silently dropped, mirroring the other
    adapters.
    """
    from google.genai import types

    if isinstance(block, TextBlock):
        return types.Part.from_text(text=block.text)

    if isinstance(block, ImageBlock):
        mime = block.media_type or "image/jpeg"
        source = block.source
        if source.startswith("file://"):
            data = load_file_as_base64(source[7:]).split(",", 1)[-1]
            return types.Part.from_bytes(data=data.encode("ascii"), mime_type=mime)
        # Hosted URI (gs:// or https://) — pass through as-is.
        return types.Part.from_uri(file_uri=source, mime_type=mime)

    if isinstance(block, DocumentBlock):
        if block.source.startswith("file://"):
            data = load_file_as_base64(block.source[7:]).split(",", 1)[-1]
            return types.Part.from_bytes(
                data=data.encode("ascii"), mime_type="application/pdf"
            )
        return types.Part.from_uri(file_uri=block.source, mime_type="application/pdf")

    if isinstance(block, AudioBlock):
        return types.Part.from_text(
            text=f"[Audio not supported by Gemini: {block.source}]"
        )

    if isinstance(block, VideoBlock):
        return types.Part.from_text(
            text=f"[Video not supported by Gemini: {block.source}]"
        )

    if isinstance(block, (ToolUseBlock, ToolResultBlock)):
        raise TypeError(
            f"ToolUseBlock/ToolResultBlock should not reach _convert_block. "
            f"Got: {type(block)}"
        )

    return types.Part.from_text(text=f"[Unsupported block: {type(block).__name__}]")


def _convert_tools(tools: list[ToolDefinition]) -> "list[types.Tool]":
    """Converts Phoson tools to Gemini Tool objects."""
    from google.genai import types

    declarations = []
    for t in tools:
        declarations.append(
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                # google-genai accepts a plain JSON-schema dict and validates
                # it into a Schema model at runtime.
                parameters=t.parameters,  # pyright: ignore[reportArgumentType]
            )
        )
    return [types.Tool(function_declarations=declarations)]


class GeminiChat(BaseLLMChat):
    """Adapter for Google Gemini API using the ``google-genai`` SDK.

    Args:
        api_key: Gemini API key. Defaults to ``GEMINI_API_KEY`` env var.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        self._client = None

    def _get_client(self) -> "genai.Client":
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def __repr__(self) -> str:
        return "GeminiChat()"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        from google.genai import types

        client = self._get_client()

        system_instruction = config.system
        if not system_instruction:
            for msg in messages:
                if msg.role == "system":
                    system_instruction = (
                        msg.content if isinstance(msg.content, str) else None
                    )
                    break

        gemini_messages = _convert_messages(messages)

        gemini_tools = _convert_tools(tools) if tools else None
        generate_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=config.temperature,
            max_output_tokens=config.max_tokens,
            # The SDK's tools param expects a list of a wider union type;
            # list invariance makes a plain list[Tool] a nominal mismatch.
            tools=gemini_tools,  # pyright: ignore[reportArgumentType]
        )

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        text_acc = ""
        has_tool_calls = False
        usage = None

        try:
            async for chunk in await client.aio.models.generate_content_stream(
                model=config.model,
                contents=gemini_messages,
                config=generate_config,
            ):
                if chunk.usage_metadata:
                    usage = chunk.usage_metadata

                for candidate in chunk.candidates or []:
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if part.text:
                                text_acc += part.text
                                yield TokenEvent(content=part.text)

                            if part.function_call:
                                has_tool_calls = True
                                # Note: Gemini SDK handles tool calls slightly
                                # differently in stream. This is a simplified version.
                                yield ToolCallEvent(
                                    index=0,  # Simplified
                                    tool_call_id=part.function_call.id or "call",
                                    tool_name=part.function_call.name or "",
                                    args=part.function_call.args or {},
                                )

        except Exception as e:
            from phoson_llm.schemas import ErrorEvent

            yield ErrorEvent(message=str(e), code="provider_error", retryable=False)
            return

        if usage:
            input_tokens = usage.prompt_token_count or 0
            output_tokens = usage.candidates_token_count or 0
            # Gemini prompt caching info
            cache_read = getattr(usage, "cached_content_token_count", 0) or 0

            cost_usd, cost_known = calculate_cost(
                model=config.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                provider="google",
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(
                    input=input_tokens, output=output_tokens, cache_read=cache_read
                ),
                cost_usd=cost_usd,
                cost_known=cost_known,
            )

        yield LLMDoneEvent(content=text_acc, has_tool_calls=has_tool_calls)
