from phoson_llm.chats.grok import GrokChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat
from phoson_llm.chats.base import BaseLLMChat

GROK_BASE = "https://api.x.ai/v1"

def test_is_openai_compatible_subclass():
    chat = GrokChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert isinstance(chat, BaseLLMChat)

def test_default_base_url():
    chat = GrokChat(api_key="test-key")
    assert chat._base_url == GROK_BASE

def test_falls_back_to_xai_api_key_env(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "env-key")
    chat = GrokChat()
    assert chat._client.api_key == "env-key"

def test_repr_includes_grok():
    chat = GrokChat(api_key="test-key")
    assert "Grok" in repr(chat)
