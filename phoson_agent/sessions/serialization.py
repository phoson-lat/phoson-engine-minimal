import datetime
from typing import Any

from phoson_llm.schemas import Message, TextBlock, ToolUseBlock, ToolResultBlock
from phoson_agent.sessions.models import ConversationNode


def block_to_dict(block: TextBlock | ToolUseBlock | ToolResultBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {
            "type": "text",
            "text": block.text,
        }
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "tool_call_id": block.tool_call_id,
            "tool_name": block.tool_name,
            "args": block.args,
        }
    return {
        "type": "tool_result",
        "tool_call_id": block.tool_call_id,
        "result": block.result,
        "error": block.error,
    }


def block_from_dict(data: dict[str, Any]) -> TextBlock | ToolUseBlock | ToolResultBlock:
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
    raise ValueError(f"Unknown content block type: {btype}")


def message_to_dict(message: Message) -> dict[str, Any]:
    if isinstance(message.content, str):
        content: str | list[dict[str, Any]] = message.content
    else:
        content = [block_to_dict(block) for block in message.content]
    return {
        "role": message.role,
        "content": content,
    }


def message_from_dict(data: dict[str, Any]) -> Message:
    raw_content = data["content"]
    if isinstance(raw_content, str):
        content: str | list[TextBlock | ToolUseBlock | ToolResultBlock] = raw_content
    else:
        content = [block_from_dict(block) for block in raw_content]
    return Message(role=data["role"], content=content)


def node_to_dict(node: ConversationNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "parent_id": node.parent_id,
        "message": message_to_dict(node.message),
        "created_at": node.created_at.isoformat(),
        "metadata": node.metadata,
    }


def node_from_dict(data: dict[str, Any]) -> ConversationNode:
    return ConversationNode(
        id=data["id"],
        parent_id=data.get("parent_id"),
        message=message_from_dict(data["message"]),
        created_at=datetime.datetime.fromisoformat(data["created_at"]),
        metadata=dict(data.get("metadata", {})),
    )
