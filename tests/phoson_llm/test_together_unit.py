from phoson_llm.chats.together import TogetherChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_together_defaults():
    chat = TogetherChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == "https://api.together.xyz/v1"
    assert "Together" in repr(chat)
