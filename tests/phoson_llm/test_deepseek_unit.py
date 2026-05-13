from phoson_llm.chats.deepseek import DeepSeekChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_deepseek_defaults():
    chat = DeepSeekChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == "https://api.deepseek.com/v1"
    assert "DeepSeek" in repr(chat)
