import datetime
from typing import Any

from phoson_llm.schemas import (
    Message,
    TextBlock,
    AudioBlock,
    ImageBlock,
    VideoBlock,
    ContentBlock,
    ToolUseBlock,
    DocumentBlock,
    ToolResultBlock,
)
from phoson_agent.sessions.models import ConversationNode, ConversationTree


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """Serialize a content block to a dictionary.

    Multimodal blocks (image/audio/video/document) are persisted with
    their full source spec so an attached file or remote URL can be
    re-loaded when the session is replayed.

    Args:
        block: Any content block from :data:`phoson_llm.schemas.ContentBlock`.

    Returns:
        Dictionary representation of the block.

    Raises:
        TypeError: For unrecognised block types — this should be unreachable
            because ``ContentBlock`` is a closed union.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "tool_call_id": block.tool_call_id,
            "tool_name": block.tool_name,
            "args": block.args,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_call_id": block.tool_call_id,
            "result": block.result,
            "error": block.error,
        }
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": block.source,
            "detail": block.detail,
            "media_type": block.media_type,
        }
    if isinstance(block, AudioBlock):
        return {
            "type": "audio",
            "source": block.source,
            "format": block.format,
            "duration_ms": block.duration_ms,
        }
    if isinstance(block, VideoBlock):
        return {
            "type": "video",
            "source": block.source,
            "sampling_interval_ms": block.sampling_interval_ms,
        }
    if isinstance(block, DocumentBlock):
        return {
            "type": "document",
            "source": block.source,
            "pages": block.pages,
        }
    raise TypeError(f"Unsupported content block type: {type(block).__name__}")


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    """Deserialize a content block from a dictionary.

    Args:
        data: Dictionary representation of a content block, as produced by
            :func:`block_to_dict`.

    Returns:
        The deserialized content block.

    Raises:
        ValueError: If block type is unknown.
    """
    btype = data["type"]
    if btype == "text":
        return TextBlock(text=data["text"])
    if btype == "tool_use":
        return ToolUseBlock(
            tool_call_id=data["tool_call_id"],
            tool_name=data["tool_name"],
            args=data.get("args", {}),
        )
    if btype == "tool_result":
        return ToolResultBlock(
            tool_call_id=data["tool_call_id"],
            result=data.get("result", ""),
            error=bool(data.get("error", False)),
        )
    if btype == "image":
        return ImageBlock(
            source=data["source"],
            detail=data.get("detail", "auto"),
            media_type=data.get("media_type"),
        )
    if btype == "audio":
        return AudioBlock(
            source=data["source"],
            format=data.get("format", "wav"),
            duration_ms=data.get("duration_ms"),
        )
    if btype == "video":
        return VideoBlock(
            source=data["source"],
            sampling_interval_ms=int(data.get("sampling_interval_ms", 2000)),
        )
    if btype == "document":
        return DocumentBlock(
            source=data["source"],
            pages=data.get("pages"),
        )
    raise ValueError(f"Unknown content block type: {btype}")


def message_to_dict(message: Message) -> dict[str, Any]:
    """Serialize a Message to a dictionary."""
    if isinstance(message.content, str):
        content: str | list[dict[str, Any]] = message.content
    else:
        content = [block_to_dict(block) for block in message.content]
    return {
        "role": message.role,
        "content": content,
    }


def message_from_dict(data: dict[str, Any]) -> Message:
    """Deserialize a Message from a dictionary."""
    raw_content = data["content"]
    if isinstance(raw_content, str):
        content: str | list[ContentBlock] = raw_content
    else:
        content = [block_from_dict(block) for block in raw_content]
    return Message(role=data["role"], content=content)


def node_to_dict(node: ConversationNode) -> dict[str, Any]:
    """Serialize a ConversationNode to a dictionary."""
    return {
        "id": node.id,
        "parent_id": node.parent_id,
        "message": message_to_dict(node.message),
        "created_at": node.created_at.isoformat(),
        "metadata": node.metadata,
    }


def node_from_dict(data: dict[str, Any]) -> ConversationNode:
    """Deserialize a ConversationNode from a dictionary."""
    return ConversationNode(
        id=data["id"],
        parent_id=data.get("parent_id"),
        message=message_from_dict(data["message"]),
        created_at=datetime.datetime.fromisoformat(data["created_at"]),
        metadata=dict(data.get("metadata", {})),
    )


def tree_meta_to_dict(tree: ConversationTree) -> dict[str, Any]:
    """Serialize session metadata from a ConversationTree."""
    return {
        "type": "session_meta",
        "session_id": tree.session_id,
        "total_cost": tree.total_cost,
        "total_tokens": tree.total_tokens,
        "total_input_tokens": tree.total_input_tokens,
        "total_output_tokens": tree.total_output_tokens,
        "step_count": tree.step_count,
        "last_model": tree.last_model,
        "title": tree.title,
    }


def apply_tree_meta(tree: ConversationTree, data: dict[str, Any]) -> None:
    """Apply session metadata to a ConversationTree."""
    # F-34: a legacy record carries only the ``total_tokens`` sum, not the
    # split. Back-fill output from the sum (input stays 0) so a resumed
    # legacy session shows its tokens under "out" instead of dropping to 0.
    has_split = "total_input_tokens" in data or "total_output_tokens" in data
    input_tokens = int(data.get("total_input_tokens", 0))
    output_tokens = int(data.get("total_output_tokens", 0))
    if not has_split:
        output_tokens = int(data.get("total_tokens", 0))
    tree.update_session_meta(
        total_cost=float(data.get("total_cost", 0.0)),
        total_tokens=int(data.get("total_tokens", 0)),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        step_count=int(data.get("step_count", 0)),
        last_model=data.get("last_model"),
        title=data.get("title"),
    )
