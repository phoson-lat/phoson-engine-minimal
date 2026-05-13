import pytest
try:
    import google.genai
except ImportError:
    google = None

pytestmark = pytest.mark.skipif(google is None, reason="google-genai not installed")

from phoson_llm.chats.gemini import GeminiChat
from phoson_llm.chats.base import BaseLLMChat

def test_is_base_llm_chat_subclass():
    chat = GeminiChat(api_key="test-key")
    assert isinstance(chat, BaseLLMChat)

def test_default_api_key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    chat = GeminiChat()
    assert chat._api_key == "env-key"

def test_repr_includes_gemini():
    chat = GeminiChat(api_key="test-key")
    assert "Gemini" in repr(chat)
