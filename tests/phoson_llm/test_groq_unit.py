from phoson_llm.chats.groq import GroqChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_groq_defaults():
    chat = GroqChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == "https://api.groq.com/openai/v1"
    assert "Groq" in repr(chat)
