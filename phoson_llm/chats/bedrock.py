"""AWS Bedrock adapter using the boto3 Converse API."""

import os
import json
import asyncio
import warnings
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
    ImageBlock,
    JsonObject,
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
    ToolCallDeltaEvent,
)
from phoson_llm.chats.base import BaseLLMChat

if TYPE_CHECKING:
    pass


# ─── Message / tool conversion Phoson → Bedrock ──────────────────────────────


def _convert_block_bedrock(block: Any) -> dict[str, Any]:
    """Convert a single Phoson ContentBlock to a Bedrock content block dict."""
    if isinstance(block, TextBlock):
        return {"text": block.text}

    if isinstance(block, ImageBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path)
            if data is None:
                return {"text": missing_attachment_placeholder("image", path)}
            b64 = data.split(",", 1)[-1]
            media = block.media_type or "image/jpeg"
            return {"image": {"format": media, "source": {"bytes": b64}}}
        return {"text": f"[Image not directly supported by Bedrock: {source}]"}

    if isinstance(block, (ToolUseBlock, ToolResultBlock)):
        # Handled by the message-level converter (they map to Bedrock's
        # ``toolUse`` / ``toolResult`` content blocks, not generic blocks).
        raise TypeError(
            f"ToolUseBlock/ToolResultBlock should not reach _convert_block_bedrock. "
            f"Got: {type(block)}"
        )

    return {"text": f"[Unsupported block: {type(block).__name__}]"}


def _dump_args(args: dict[str, Any]) -> str:
    """Serialize tool arguments to the JSON string Bedrock expects."""
    return json.dumps(args)


def _convert_messages_bedrock(messages: list[Message]) -> list[dict[str, Any]]:
    """Converts Phoson messages to Bedrock Converse message dicts.

    Handles the tool-calling round-trip: an assistant message carrying
    ``ToolUseBlock``s becomes an assistant message with ``toolUse`` content
    blocks; a user message carrying ``ToolResultBlock``s becomes a user
    message with ``toolResult`` content blocks.
    """
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            continue

        role = "user" if msg.role == "user" else "assistant"

        if isinstance(msg.content, str):
            result.append({"role": role, "content": [{"text": msg.content}]})
            continue

        blocks: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": block.tool_call_id,
                            "name": block.tool_name,
                            "input": block.args or {},
                        }
                    }
                )
            elif isinstance(block, ToolResultBlock):
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": block.tool_call_id,
                            "content": [{"text": block.result}],
                            "status": "error" if block.error else "success",
                        }
                    }
                )
            else:
                blocks.append(_convert_block_bedrock(block))

        if blocks:
            result.append({"role": role, "content": blocks})

    return result


def _convert_tools_bedrock(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Converts Phoson ``ToolDefinition`` objects to Bedrock ``toolSpec`` dicts."""
    return [
        {
            "toolSpec": {
                "name": t.name,
                "description": t.description,
                "inputSchema": {"json": t.parameters},
            }
        }
        for t in tools
    ]


def _extract_system_text(messages: list[Message]) -> str | None:
    """Extract the first system message's text content, if any."""
    for msg in messages:
        if msg.role == "system" and isinstance(msg.content, str):
            return msg.content
    return None


def _finalize_tool_calls(
    tool_ids: dict[int, str],
    tool_names: dict[int, str],
    tool_args_acc: dict[int, str],
) -> list[ToolCallEvent]:
    """Build a :class:`ToolCallEvent` per accumulated Bedrock tool call."""
    events: list[ToolCallEvent] = []
    for idx in sorted(tool_args_acc):
        raw = tool_args_acc[idx]
        args: JsonObject
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            warnings.warn(
                f"Could not parse tool args JSON from Bedrock stream "
                f"(tool={tool_names.get(idx)!r}); stored as _raw.",
                UserWarning,
                stacklevel=2,
            )
            args = {"_raw": raw}
        events.append(
            ToolCallEvent(
                index=idx,
                tool_call_id=tool_ids.get(idx, f"tool_{idx}"),
                tool_name=tool_names.get(idx, ""),
                args=args,
            )
        )
    return events


class BedrockChat(BaseLLMChat):
    """Adapter for AWS Bedrock using the ``boto3`` Converse API.

    Args:
        region_name: AWS region. Defaults to ``AWS_DEFAULT_REGION`` or ``us-east-1``.
    """

    def __init__(self, region_name: str | None = None) -> None:
        self._region_name = region_name or os.environ.get(
            "AWS_DEFAULT_REGION", "us-east-1"
        )
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "bedrock-runtime", region_name=self._region_name
            )
        return self._client

    def __repr__(self) -> str:
        return f"BedrockChat(region_name={self._region_name!r})"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        client = self._get_client()

        bedrock_messages = _convert_messages_bedrock(messages)
        system: list[dict[str, str]] = []
        system_text = config.system or _extract_system_text(messages)
        if system_text:
            system = [{"text": system_text}]

        tool_config: dict[str, Any] | None = None
        if tools:
            tool_config = {"tools": _convert_tools_bedrock(tools)}

        temperature = config.temperature if config.temperature is not None else 0.7
        inference_config = {
            "temperature": temperature,
            "maxTokens": config.max_tokens or 2048,
        }

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        text_acc = ""
        has_tool_calls = False
        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_args_acc: dict[int, str] = {}
        stop_reason: object = None
        usage: TokenUsage | None = None

        try:
            loop = asyncio.get_event_loop()
            if hasattr(client, "converse_stream"):
                response = await loop.run_in_executor(
                    None,
                    lambda: client.converse_stream(
                        modelId=config.model,
                        messages=bedrock_messages,
                        system=system,
                        toolConfig=tool_config,
                        inferenceConfig=inference_config,
                    ),
                )
            else:
                # Fallback for older boto3 without ``converse_stream``.
                warnings.warn(
                    "boto3.client('bedrock-runtime') has no converse_stream; "
                    "falling back to non-streaming converse().",
                    UserWarning,
                    stacklevel=2,
                )
                response = await loop.run_in_executor(
                    None,
                    lambda: client.converse(
                        modelId=config.model,
                        messages=bedrock_messages,
                        system=system,
                        toolConfig=tool_config,
                        inferenceConfig=inference_config,
                    ),
                )

            if isinstance(response, dict) and "stream" in response:
                # ── Streaming path (boto3 converse_stream) ─────────────────
                for event in response["stream"]:
                    if "contentDelta" in event:
                        delta = event["contentDelta"]
                        text = delta.get("text")
                        if text:
                            text_acc += text
                            yield TokenEvent(content=text)

                    elif "toolUse" in event:
                        tu = event["toolUse"]
                        has_tool_calls = True
                        chunk_json = ""
                        if "content" in tu:
                            chunk_json = tu["content"].get("json", "") or ""
                        if "toolUseId" in tu:
                            # Start of a new tool call: register it and index
                            # it by order of appearance.
                            idx = len(tool_ids)
                            tool_ids[idx] = tu["toolUseId"]
                            tool_names[idx] = tu.get("name", "")
                            tool_args_acc[idx] = ""
                            if chunk_json:
                                tool_args_acc[idx] += chunk_json
                                yield ToolCallDeltaEvent(
                                    index=idx,
                                    tool_name=tool_names[idx],
                                    args_chunk=chunk_json,
                                )
                        elif "content" in tu:
                            # Continuation delta for the most recent tool call.
                            idx = max(tool_args_acc) if tool_args_acc else 0
                            tool_args_acc[idx] = tool_args_acc.get(idx, "") + chunk_json
                            if chunk_json:
                                yield ToolCallDeltaEvent(
                                    index=idx,
                                    tool_name=tool_names.get(idx, ""),
                                    args_chunk=chunk_json,
                                )

                    elif "metadata" in event:
                        meta = event["metadata"]
                        stop_reason = meta.get("stopReason")
                        u = meta.get("usage")
                        if u:
                            usage = TokenUsage(
                                input=u.get("inputTokens", 0) or 0,
                                output=u.get("outputTokens", 0) or 0,
                            )

            else:
                # ── Non-streaming fallback (boto3 converse) ────────────────
                out = response["output"]["message"]["content"]
                text_parts = [c.get("text", "") for c in out if "text" in c]
                text = "".join(text_parts)
                if text:
                    text_acc = text
                    yield TokenEvent(content=text)
                for c in out:
                    if "toolUse" in c:
                        tu = c["toolUse"]
                        has_tool_calls = True
                        idx = len(tool_ids)
                        tool_ids[idx] = tu.get("toolUseId", f"tool_{idx}")
                        tool_names[idx] = tu.get("name", "")
                        tool_args_acc[idx] = _dump_args(tu.get("input", {}))
                stop_reason = response.get("stop_reason")
                u = response.get("usage")
                if u:
                    usage = TokenUsage(
                        input=u.get("inputTokens", 0) or 0,
                        output=u.get("outputTokens", 0) or 0,
                    )

            # Emit complete tool calls after the stream closes.
            for event in _finalize_tool_calls(tool_ids, tool_names, tool_args_acc):
                yield event

            if usage is not None:
                cost_usd, cost_known = calculate_cost(
                    model=config.model,
                    input_tokens=usage.input,
                    output_tokens=usage.output,
                    provider="bedrock",
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
                # F-13: Converse reports the stop reason at the response top
                # level (``end_turn`` / ``max_tokens`` / ``stop_sequence`` /
                # ``tool_use``).
                stop_reason=normalize_stop_reason(stop_reason, provider="bedrock"),
            )

        except Exception as e:
            from phoson_llm.schemas import ErrorEvent

            yield ErrorEvent(message=str(e), code="provider_error", retryable=False)
            return
