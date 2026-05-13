from phoson_llm.chats.vllm import VLLMChat, VLLM_DEFAULT_BASE_URL
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

def test_vllm_defaults():
    chat = VLLMChat()
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == VLLM_DEFAULT_BASE_URL
    assert "VLLM" in repr(chat)

def test_vllm_custom_base_url():
    chat = VLLMChat(base_url="http://my-vllm:8000/v1")
    assert chat._base_url == "http://my-vllm:8000/v1"
