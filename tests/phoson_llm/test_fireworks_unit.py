from phoson_llm.chats.fireworks import FireworksChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_fireworks_defaults():
    chat = FireworksChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == "https://api.fireworks.ai/inference/v1"
    assert "Fireworks" in repr(chat)
