from phoson_llm.chats.lmstudio import LMStudioChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat


def test_default_base_url():
    chat = LMStudioChat()
    assert chat._base_url == "http://localhost:1234/v1"


def test_custom_base_url():
    chat = LMStudioChat(base_url="http://192.168.1.5:1234/v1")
    assert chat._base_url == "http://192.168.1.5:1234/v1"


def test_is_openai_compatible_subclass():
    chat = LMStudioChat()
    assert isinstance(chat, OpenAICompatibleChat)


def test_repr_includes_lmstudio():
    chat = LMStudioChat()
    assert "LMStudio" in repr(chat)
