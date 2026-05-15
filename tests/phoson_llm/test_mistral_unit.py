import pytest

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.mistral import MistralChat

try:
    import mistralai
except ImportError:
    mistralai = None

pytestmark = pytest.mark.skipif(mistralai is None, reason="mistralai not installed")


def test_is_base_llm_chat_subclass():
    chat = MistralChat(api_key="test-key")
    assert isinstance(chat, BaseLLMChat)


def test_repr_includes_mistral():
    chat = MistralChat(api_key="test-key")
    assert "Mistral" in repr(chat)
