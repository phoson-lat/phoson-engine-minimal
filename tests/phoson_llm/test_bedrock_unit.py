import pytest
try:
    import boto3
except ImportError:
    boto3 = None

pytestmark = pytest.mark.skipif(boto3 is None, reason="boto3 not installed")

from phoson_llm.chats.bedrock import BedrockChat
from phoson_llm.chats.base import BaseLLMChat

def test_is_base_llm_chat_subclass():
    chat = BedrockChat(region_name="us-east-1")
    assert isinstance(chat, BaseLLMChat)

def test_repr_includes_bedrock():
    chat = BedrockChat(region_name="us-east-1")
    assert "Bedrock" in repr(chat)
