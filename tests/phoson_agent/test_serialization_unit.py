import datetime

import pytest

from phoson_llm.schemas import Message, TextBlock, ToolUseBlock, ToolResultBlock
from phoson_agent.sessions.models import ConversationNode, ConversationTree
from phoson_agent.sessions.serialization import (
    node_to_dict,
    block_to_dict,
    node_from_dict,
    apply_tree_meta,
    block_from_dict,
    message_to_dict,
    message_from_dict,
    tree_meta_to_dict,
)


def test_block_to_dict_text() -> None:
    block = TextBlock(text="Hello, world!")
    result = block_to_dict(block)
    assert result == {"type": "text", "text": "Hello, world!"}


def test_block_to_dict_tool_use() -> None:
    block = ToolUseBlock(
        tool_call_id="call_123",
        tool_name="get_weather",
        args={"city": "NYC"},
    )
    result = block_to_dict(block)
    assert result == {
        "type": "tool_use",
        "tool_call_id": "call_123",
        "tool_name": "get_weather",
        "args": {"city": "NYC"},
    }


def test_block_to_dict_tool_result() -> None:
    block = ToolResultBlock(
        tool_call_id="call_123",
        result="Sunny, 25C",
        error=False,
    )
    result = block_to_dict(block)
    assert result == {
        "type": "tool_result",
        "tool_call_id": "call_123",
        "result": "Sunny, 25C",
        "error": False,
    }


def test_block_from_dict_text() -> None:
    data = {"type": "text", "text": "Hello"}
    block = block_from_dict(data)
    assert isinstance(block, TextBlock)
    assert block.text == "Hello"


def test_block_from_dict_tool_use() -> None:
    data = {
        "type": "tool_use",
        "tool_call_id": "call_456",
        "tool_name": "search",
        "args": {"query": "test"},
    }
    block = block_from_dict(data)
    assert isinstance(block, ToolUseBlock)
    assert block.tool_call_id == "call_456"
    assert block.tool_name == "search"
    assert block.args == {"query": "test"}


def test_block_from_dict_tool_result() -> None:
    data = {
        "type": "tool_result",
        "tool_call_id": "call_789",
        "result": "Result text",
        "error": False,
    }
    block = block_from_dict(data)
    assert isinstance(block, ToolResultBlock)
    assert block.tool_call_id == "call_789"
    assert block.result == "Result text"
    assert block.error is False


def test_block_from_dict_raises_unknown_type() -> None:
    data = {"type": "unknown_type"}
    with pytest.raises(ValueError, match="Unknown content block type"):
        block_from_dict(data)


def test_message_to_dict_string_content() -> None:
    msg = Message(role="user", content="Hello there")
    result = message_to_dict(msg)
    assert result == {"role": "user", "content": "Hello there"}


def test_message_to_dict_list_content() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="Hi"),
            ToolUseBlock(tool_call_id="c1", tool_name="t1", args={}),
        ],
    )
    result = message_to_dict(msg)
    assert result["role"] == "assistant"
    assert len(result["content"]) == 2
    assert result["content"][0] == {"type": "text", "text": "Hi"}
    assert result["content"][1] == {
        "type": "tool_use",
        "tool_call_id": "c1",
        "tool_name": "t1",
        "args": {},
    }


def test_message_from_dict_string_content() -> None:
    data = {"role": "user", "content": "Hello"}
    msg = message_from_dict(data)
    assert msg.role == "user"
    assert isinstance(msg.content, str)
    assert msg.content == "Hello"


def test_message_from_dict_list_content() -> None:
    data = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Hello"},
        ],
    }
    msg = message_from_dict(data)
    assert msg.role == "assistant"
    assert isinstance(msg.content, list)
    assert isinstance(msg.content[0], TextBlock)


def test_message_roundtrip() -> None:
    original = Message(
        role="user",
        content=[
            TextBlock(text="Hello"),
            ToolUseBlock(
                tool_call_id="call_1", tool_name="test", args={"arg": "value"}
            ),
        ],
    )
    dict_form = message_to_dict(original)
    restored = message_from_dict(dict_form)
    assert original.role == restored.role
    assert len(original.content) == len(restored.content)


def test_node_to_dict_and_back() -> None:
    node = ConversationNode(
        id="node_abc",
        parent_id="node_xyz",
        message=Message(role="user", content="Test message"),
        created_at=datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC),
        metadata={"label": "test"},
    )
    dict_form = node_to_dict(node)
    assert dict_form["id"] == "node_abc"
    assert dict_form["parent_id"] == "node_xyz"
    assert dict_form["message"]["role"] == "user"
    assert dict_form["created_at"] == "2026-01-01T12:00:00+00:00"
    assert dict_form["metadata"] == {"label": "test"}


def test_node_from_dict() -> None:
    data = {
        "id": "node_def",
        "parent_id": None,
        "message": {"role": "assistant", "content": "Response"},
        "created_at": "2026-06-15T08:30:00+00:00",
        "metadata": {"important": True},
    }
    node = node_from_dict(data)
    assert node.id == "node_def"
    assert node.parent_id is None
    assert node.message.role == "assistant"
    assert node.metadata == {"important": True}


def test_tree_meta_to_dict() -> None:
    tree = ConversationTree.new(session_id="session_123")
    tree.update_session_meta(
        total_cost=1.23,
        total_tokens=5000,
        step_count=10,
        last_model="gpt-4o",
    )
    meta = tree_meta_to_dict(tree)
    assert meta["type"] == "session_meta"
    assert meta["session_id"] == "session_123"
    assert meta["total_cost"] == 1.23
    assert meta["total_tokens"] == 5000
    assert meta["step_count"] == 10
    assert meta["last_model"] == "gpt-4o"


def test_apply_tree_meta() -> None:
    tree = ConversationTree.new(session_id="session_456")
    data = {
        "total_cost": 2.5,
        "total_tokens": 1000,
        "step_count": 5,
        "last_model": "claude-3",
    }
    apply_tree_meta(tree, data)
    assert tree.total_cost == 2.5
    assert tree.total_tokens == 1000
    assert tree.step_count == 5
    assert tree.last_model == "claude-3"


def test_apply_tree_meta_with_missing_fields() -> None:
    tree = ConversationTree.new(session_id="session_789")
    tree.update_session_meta(total_cost=1.0, total_tokens=100)
    data = {}
    apply_tree_meta(tree, data)
    assert tree.total_cost == 0.0
    assert tree.total_tokens == 0
    assert tree.step_count == 0
    assert tree.last_model is None
