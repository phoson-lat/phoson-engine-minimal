"""Google Gemini adapter using the native SDK."""

import os
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator

from phoson_llm.utils import (
    load_file_as_base64,
    normalize_stop_reason,
    missing_attachment_placeholder,
)
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

    # Gemini's ``function_response`` part requires the *function name*, but a
    # Phoson ``ToolResultBlock`` only carries the ``tool_call_id``. Build a
    # tool_call_id → tool_name map from the assistant ``ToolUseBlock``s (which
    # always precede their results in the conversation) so each response can
    # be named correctly.
    tool_name_by_id: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg.content, str):
            continue
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                tool_name_by_id[block.tool_call_id] = block.tool_name

    gemini_messages = []
    for msg in messages:
        if msg.role == "system":
            continue

        parts = []
        if isinstance(msg.content, str):
            parts.append(types.Part.from_text(text=msg.content))
        else:
            for block in msg.content:
                part_or_text = _convert_block(types, block, tool_name_by_id)
                parts.append(part_or_text)

        role = "user" if msg.role == "user" else "model"
        gemini_messages.append(types.Content(role=role, parts=parts))

    return gemini_messages


def _convert_block(types: Any, block: Any, tool_name_by_id: dict[str, str]) -> Any:
    """Convert a single Phoson ContentBlock to a Gemini Part.

    Local ``file://`` sources are read and base64-encoded inline
    (Gemini's ``file_uri`` only accepts Google-hosted resources, never
    local paths). Unsupported block types become a visible text
    placeholder instead of being silently dropped, mirroring the other
    adapters.

    ``tool_name_by_id`` maps a ``ToolUseBlock``'s ``tool_call_id`` to its
    name; it is needed to name the ``function_response`` parts produced from
    ``ToolResultBlock``s, which only carry the id.
    """
    from google.genai import types

    if isinstance(block, TextBlock):
        return types.Part.from_text(text=block.text)

    if isinstance(block, ImageBlock):
        mime = block.media_type or "image/jpeg"
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path)
            if data is None:
                return types.Part.from_text(
                    text=missing_attachment_placeholder("image", path)
                )
            data = data.split(",", 1)[-1]
            return types.Part.from_bytes(data=data.encode("ascii"), mime_type=mime)
        # Hosted URI (gs:// or https://) — pass through as-is.
        return types.Part.from_uri(file_uri=source, mime_type=mime)

    if isinstance(block, DocumentBlock):
        if block.source.startswith("file://"):
            path = block.source[7:]
            data = load_file_as_base64(path)
            if data is None:
                return types.Part.from_text(
                    text=missing_attachment_placeholder("document", path)
                )
            data = data.split(",", 1)[-1]
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

    if isinstance(block, ToolUseBlock):
        # An assistant turn that requested a tool becomes a ``function_call``
        # part. Gemini correlates the later ``function_response`` back to this
        # call via the id, so carry the Phoson tool_call_id through.
        return types.Part(
            function_call=types.FunctionCall(
                name=block.tool_name,
                args=block.args or {},
                id=block.tool_call_id,
            )
        )

    if isinstance(block, ToolResultBlock):
        # A user turn carrying a tool result becomes a ``function_response``
        # part. ``ToolResultBlock`` only carries the ``tool_call_id``; the
        # function name is looked up from the matching ``ToolUseBlock``.
        # ``from_function_response`` does not accept an ``id``, so build the
        # ``FunctionResponse`` (which does carry the id) and wrap it by hand.
        function_response = types.FunctionResponse(
            name=tool_name_by_id.get(block.tool_call_id, ""),
            response={"result": block.result},
            id=block.tool_call_id,
        )
        return types.Part(function_response=function_response)

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
        # Gemini emits each ``function_call`` part as a *complete* tool call
        # (unlike OpenAI, where args stream in fragments). A single response
        # can carry several tool calls, so we assign each one a distinct,
        # incremental index rather than hardcoding 0 (which used to make the
        # 2nd+ call overwrite the 1st).
        tool_call_index = 0
        # F-13: Gemini carries the stop reason on each candidate as
        # ``finish_reason`` (a FinishReason enum). Keep the last non-None one.
        stop_reason: object = None

        try:
            async for chunk in await client.aio.models.generate_content_stream(
                model=config.model,
                contents=gemini_messages,
                config=generate_config,
            ):
                if chunk.usage_metadata:
                    usage = chunk.usage_metadata

                for candidate in chunk.candidates or []:
                    cand_finish = getattr(candidate, "finish_reason", None)
                    if cand_finish is not None:
                        stop_reason = cand_finish
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if part.text:
                                text_acc += part.text
                                yield TokenEvent(content=part.text)

                            if part.function_call:
                                has_tool_calls = True
                                fc = part.function_call
                                yield ToolCallEvent(
                                    index=tool_call_index,
                                    tool_call_id=fc.id or f"call_{tool_call_index}",
                                    tool_name=fc.name or "",
                                    args=fc.args or {},
                                )
                                tool_call_index += 1

        except Exception as e:
            from phoson_llm.utils import (
                CONTEXT_LENGTH_ERROR_CODE,
                is_context_length_error,
            )
            from phoson_llm.schemas import ErrorEvent

            status = getattr(e, "code", None)
            if not isinstance(status, int):
                status = None
            code = "provider_error"
            if is_context_length_error(status, str(e)):
                code = CONTEXT_LENGTH_ERROR_CODE
            yield ErrorEvent(message=str(e), code=code, retryable=False)
            return

        if usage:
            input_tokens = usage.prompt_token_count or 0
            # The SDK renamed the output-token field across versions
            # (``candidates_token_count`` → ``response_token_count``); probe
            # both so a missing attribute does not break the stream.
            output_tokens = (
                getattr(usage, "response_token_count", None)
                or getattr(usage, "candidates_token_count", None)
                or 0
            )
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

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
            stop_reason=normalize_stop_reason(stop_reason, provider="google"),
        )
