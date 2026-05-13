from phoson_llm.chats.cohere import CohereChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat


def test_cohere_defaults():
    chat = CohereChat(api_key="test-key")
    assert isinstance(chat, OpenAICompatibleChat)
    # Note: Cohere base_url is https://api.cohere.com/v1 but
    # OpenAICompatibleChat strips trailing slash
    assert chat._base_url == "https://api.cohere.com/v1"
    assert "Cohere" in repr(chat)
