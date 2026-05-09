"""Shared helpers for OpenAI-compatible chat adapters."""

import json
import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phoson_llm.schemas import Message, ContentBlock, ToolDefinition


def _convert_content_block(block: "ContentBlock") -> dict:
    """Converts a Phoson ContentBlock to OpenAI-compatible API format."""
    from phoson_llm.utils import load_file_as_base64
    from phoson_llm.schemas import (
        TextBlock,
        AudioBlock,
        ImageBlock,
        VideoBlock,
        ToolUseBlock,
        ToolResultBlock,
    )

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
            "text": f"[Video not directly supported by this provider: {block.source}]",
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


def _convert_messages(messages: list["Message"]) -> list[dict]:
    """Converts Phoson messages to OpenAI-compatible format."""
    from phoson_llm.schemas import TextBlock, ToolUseBlock, ToolResultBlock

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


def _convert_tools(tools: list["ToolDefinition"]) -> list[dict]:
    """Converts ToolDefinition to OpenAI-compatible tools format."""
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