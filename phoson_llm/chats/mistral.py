"""Mistral AI adapter using the native SDK."""

import os
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator

from phoson_llm.utils import normalize_stop_reason
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolDefinition,
    ToolResultBlock,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats._openai_compatible import ToolCallAccumulator

if TYPE_CHECKING:
    from mistralai.client import Mistral


def _convert_tools_mistral(tools: list[ToolDefinition]) -> list[Any]:
    """Converts Phoson ``ToolDefinition`` objects to Mistral ``Tool`` objects.

    Mistral's OpenAI-compatible tool schema is ``{"type": "function",
    "function": {name, description, parameters}}``. We build the SDK's
    ``Tool``/``Function`` models so the request is validated client-side.
    """
    from mistralai.client.models.tool import Tool
    from mistralai.client.models.function import Function

    return [
        Tool(
            type="function",
            function=Function(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            ),
        )
        for t in tools
    ]


def _convert_messages_mistral(messages: list[Message]) -> list[dict[str, Any]]:
    """Converts Phoson messages to Mistral's chat-completions message dicts.

    Handles the tool-calling round-trip: an assistant message carrying
    ``ToolUseBlock``s becomes an assistant message with ``tool_calls``; a user
    message carrying ``ToolResultBlock``s becomes one ``role="tool"`` message
    per result (Mistral, like OpenAI, reports tool results on their own
    ``tool``-role messages rather than inside the user turn).
    """
    result: list[dict[str, Any]] = []

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
        text = " ".join(b.text for b in text_blocks)

        if tool_uses:
            result.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": b.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": b.tool_name,
                                "arguments": _dump_args(b.args),
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

        # A user turn that is only tool results (no text) is already covered
        # above; a turn with text but no tool blocks falls through here.
        if not tool_uses and not tool_results and text:
            result.append({"role": msg.role, "content": text})

    return result


def _dump_args(args: dict[str, Any]) -> str:
    """Serialize tool arguments to the JSON string Mistral expects."""
    import json

    return json.dumps(args)


class MistralChat(BaseLLMChat):
    """Adapter for Mistral AI API using the ``mistralai`` SDK.

    Args:
        api_key: Mistral API key. Defaults to ``MISTRAL_API_KEY`` env var.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("MISTRAL_API_KEY") or ""
        self._client = None

    def _get_client(self) -> "Mistral":
        if self._client is None:
            from mistralai.client import Mistral

            self._client = Mistral(api_key=self._api_key)
        return self._client

    def __repr__(self) -> str:
        return "MistralChat()"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        client = self._get_client()

        mistral_messages = _convert_messages_mistral(messages)

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        text_acc = ""
        has_tool_calls = False
        tool_acc = ToolCallAccumulator()
        # F-13: Mistral (OpenAI-compatible) reports the stop reason on the
        # choice as ``finish_reason``. Probe defensively — the SDK shape has
        # varied, and a missing attribute must not break the stream.
        stop_reason: object = None

        try:
            stream_response = await client.chat.stream_async(
                model=config.model,
                messages=mistral_messages,  # pyright: ignore[reportArgumentType]
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                tools=_convert_tools_mistral(tools) if tools else None,
            )

            async for chunk in stream_response:
                choice = chunk.data.choices[0]
                delta = choice.delta
                delta_content = delta.content
                if isinstance(delta_content, str) and delta_content:
                    text_acc += delta_content
                    yield TokenEvent(content=delta_content)

                if delta.tool_calls:
                    has_tool_calls = True
                    for tc in delta.tool_calls:
                        for event in tool_acc.feed_delta(tc):
                            yield event

                finish = getattr(choice, "finish_reason", None)
                if finish is not None:
                    stop_reason = finish
                # Mistral finalizes a tool-call turn with ``finish_reason``
                # ``"tool_calls"`` (OpenAI-compatible). Emit the accumulated
                # complete calls at that point so consumers see them as soon
                # as the turn ends, mirroring the OpenAI-compatible adapter.
                if finish == "tool_calls":
                    for event in tool_acc.finalize():
                        yield event

                if chunk.data.usage:
                    u = chunk.data.usage
                    usage = TokenUsage(
                        input=u.prompt_tokens or 0,
                        output=u.completion_tokens or 0,
                    )
                    cost_usd, cost_known = calculate_cost(
                        model=config.model,
                        input_tokens=usage.input,
                        output_tokens=usage.output,
                        provider="mistral",
                    )
                    yield UsageEvent(
                        model=config.model,
                        usage=usage,
                        cost_usd=cost_usd,
                        cost_known=cost_known,
                    )

        except Exception as e:
            from phoson_llm.schemas import ErrorEvent

            yield ErrorEvent(message=str(e), code="provider_error", retryable=False)
            return

        # F-13: if the response was cut off at the token budget mid-tool-call,
        # the buffer still holds fragments that never got a ``tool_calls``
        # finish. Emit them as truncated calls so the agent loop can answer
        # the partial ``tool_use`` with an actionable error result instead of
        # silently dropping the model's intent.
        normalized_stop = normalize_stop_reason(stop_reason, provider="openai_compat")
        if normalized_stop == "max_tokens":
            truncated_calls = tool_acc.finalize(truncated=True)
            for event in truncated_calls:
                yield event
            if truncated_calls:
                has_tool_calls = True

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
            stop_reason=normalized_stop,
        )
