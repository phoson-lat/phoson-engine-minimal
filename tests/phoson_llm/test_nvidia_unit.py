from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.nvidia import NVIDIAChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"

def test_is_openai_compatible_subclass():
    chat = NVIDIAChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert isinstance(chat, BaseLLMChat)

def test_default_base_url():
    chat = NVIDIAChat(api_key="test-key")
    assert chat._base_url == NVIDIA_BASE

def test_falls_back_to_nvidia_api_key_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "env-key")
    chat = NVIDIAChat()
    assert chat._client.api_key == "env-key"

def test_repr_includes_nvidia():
    chat = NVIDIAChat(api_key="test-key")
    assert "NVIDIA" in repr(chat)
