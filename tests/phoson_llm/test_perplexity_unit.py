from phoson_llm.chats.perplexity import PerplexityChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_perplexity_defaults():
    chat = PerplexityChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == "https://api.perplexity.ai"
    assert "Perplexity" in repr(chat)
