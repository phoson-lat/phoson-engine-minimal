import hashlib
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
from phoson_agent.sessions.models import (
    STATUS_ACTIVE,
    ConversationNode,
    ConversationTree,
)


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
    """Serialize a Message to a dictionary.

    The turn's captured reasoning (``reasoning``) and its provider signature
    (``reasoning_signature``) are persisted so a resumed session can re-send
    the thinking to the model on the next turn (#134). Both are omitted when
    ``None`` to keep the on-disk shape unchanged for turns without reasoning.
    """
    if isinstance(message.content, str):
        content: str | list[dict[str, Any]] = message.content
    else:
        content = [block_to_dict(block) for block in message.content]
    data: dict[str, Any] = {
        "role": message.role,
        "content": content,
    }
    if message.reasoning is not None:
        data["reasoning"] = message.reasoning
    if message.reasoning_signature is not None:
        data["reasoning_signature"] = message.reasoning_signature
    return data


def message_from_dict(data: dict[str, Any]) -> Message:
    """Deserialize a Message from a dictionary."""
    raw_content = data["content"]
    if isinstance(raw_content, str):
        content: str | list[ContentBlock] = raw_content
    else:
        content = [block_from_dict(block) for block in raw_content]
    return Message(
        role=data["role"],
        content=content,
        reasoning=data.get("reasoning"),
        reasoning_signature=data.get("reasoning_signature"),
    )


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
        "status": tree.status,
        "last_run_id": tree.last_run_id,
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
    # #129: legacy meta records carry no status — default to "active" so a
    # pre-#129 session is treated as "may have died mid-run" and gets
    # orphan-checked on resume instead of being assumed clean.
    status = data.get("status") or STATUS_ACTIVE
    tree.update_session_meta(
        total_cost=float(data.get("total_cost", 0.0)),
        total_tokens=int(data.get("total_tokens", 0)),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        step_count=int(data.get("step_count", 0)),
        last_model=data.get("last_model"),
        title=data.get("title"),
        status=status,
        last_run_id=data.get("last_run_id"),
    )


# ── Orphan recovery (#129) ────────────────────────────────────────────────────

#: Marker stored in the recovery node's metadata so front ends can show
#: "⚠ recovered from orphaned run" and the idempotency check can find it.
RECOVERY_METADATA_KEY = "recovery"

#: Tool result text injected for a tool call that never completed.
ORPHAN_RECOVERY_TEXT = (
    "[Run interrupted before this tool completed. The result was lost.]"
)


def _orphan_tool_call_ids(node: ConversationNode) -> list[str]:
    """Tool-call ids on the last assistant node that lack a result.

    A node is *orphaned* when it is an assistant message carrying at least
    one ``ToolUseBlock`` whose ``tool_call_id`` is not answered by a
    ``ToolResultBlock`` in the same message. The ids are returned in
    message order (deterministic, no set-iteration nondeterminism).
    """
    message = node.message
    if message.role != "assistant" or not isinstance(message.content, list):
        return []
    result_ids = {
        block.tool_call_id
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    orphaned: list[str] = []
    for block in message.content:
        if isinstance(block, ToolUseBlock) and block.tool_call_id not in result_ids:
            orphaned.append(block.tool_call_id)
    return orphaned


def _recovery_node_id(parent_id: str, tool_call_ids: list[str]) -> str:
    """Deterministic id for the recovery node.

    Derived from the parent node id + the orphaned tool-call ids (not a
    fresh random UUID) so that re-running :func:`orphan_recovery` on an
    already-recovered path produces the *same* node id — the idempotency
    guarantee that lets a second load append nothing.
    """
    digest = hashlib.sha256(
        (parent_id + "|" + "|".join(tool_call_ids)).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def orphan_recovery(nodes: list[ConversationNode]) -> list[ConversationNode]:
    """Repair a history that ends on an unfinished tool call (#129).

    If the last node is an assistant message with ``ToolUseBlock``s that
    have no matching ``ToolResultBlock``, a user message carrying an
    error ``ToolResultBlock`` per orphaned call is appended, so the next
    LLM call does not fail with a 400 tool-pairing error.

    The recovery node is tagged with ``metadata["recovery"] = True`` so
    front ends can surface "⚠ recovered from orphaned run", and its id is
    derived deterministically from the parent + orphaned ids — calling
    this function again on an already-recovered list is a no-op
    (idempotent), which is what makes it safe to run on every resume.

    Args:
        nodes: The conversation path (root → last), as loaded from storage.

    Returns:
        The same list, possibly extended with one recovery node. The
        input list is not mutated when no recovery is needed.
    """
    if not nodes:
        return nodes

    last = nodes[-1]
    orphaned_ids = _orphan_tool_call_ids(last)
    if not orphaned_ids:
        return nodes

    # Idempotency: a recovery node for exactly this orphan set already
    # follows the last assistant node (e.g. a partial save landed between
    # the crash and the resume). Nothing to repair.
    for existing in nodes:
        if existing.metadata.get(RECOVERY_METADATA_KEY):
            if (
                existing.parent_id == last.id
                and isinstance(existing.message.content, list)
                and {
                    block.tool_call_id
                    for block in existing.message.content
                    if isinstance(block, ToolResultBlock)
                }
                == set(orphaned_ids)
            ):
                return nodes

    recovery_results = [
        ToolResultBlock(
            tool_call_id=tool_call_id,
            result=ORPHAN_RECOVERY_TEXT,
            error=True,
        )
        for tool_call_id in orphaned_ids
    ]
    recovery_node = ConversationNode(
        id=_recovery_node_id(last.id, orphaned_ids),
        parent_id=last.id,
        message=Message(role="user", content=recovery_results),
        created_at=datetime.datetime.now(datetime.UTC),
        metadata={RECOVERY_METADATA_KEY: True},
    )
    nodes.append(recovery_node)
    return nodes
