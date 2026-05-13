import pytest

from phoson_llm import build_chat
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.exceptions import PhosonLLMError
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

DEFAULT_URL = "https://api.example.com/v1"

def test_is_base_llm_chat_subclass():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert isinstance(chat, BaseLLMChat)

def test_default_base_url():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._base_url == DEFAULT_URL

def test_custom_max_tokens_key():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key",
                                max_tokens_key="max_completion_tokens")
    assert chat._max_tokens_key == "max_completion_tokens"

def test_default_max_tokens_key_is_max_tokens():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._max_tokens_key == "max_tokens"

def test_client_uses_base_url():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert str(chat._client.base_url) == DEFAULT_URL + "/"

def test_client_uses_api_key():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="sk-test-123")
    assert chat._client.api_key == "sk-test-123"

def test_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "env-key-value")
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key_env="MY_CUSTOM_KEY")
    assert chat._client.api_key == "env-key-value"

def test_default_headers_are_passed():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key",
                                default_headers={"X-Custom": "value"})
    headers = getattr(chat._client, "default_headers", {})
    assert headers.get("X-Custom") == "value"

def test_default_headers_is_not_none_when_not_provided():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    headers = getattr(chat._client, "default_headers", {})
    # OpenAI SDK v2 always has default headers (Accept, Content-Type, etc.)
    assert "Accept" in headers

def test_cost_calculator_defaults_to_none():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._cost_calculator is None

def test_extra_kwargs_defaults_to_none():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert chat._extra_kwargs is None

def test_repr_includes_provider_name():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key",
                                provider_name="MyProvider")
    assert "MyProvider" in repr(chat)

def test_repr_defaults_to_openai_compatible():
    chat = OpenAICompatibleChat(base_url=DEFAULT_URL, api_key="test-key")
    assert "OpenAICompatible" in repr(chat)

def test_build_chat_openai():
    chat = build_chat("openai", api_key="test-key")
    assert isinstance(chat, OpenAIChat)

def test_build_chat_anthropic():
    chat = build_chat("anthropic", api_key="test-key")
    assert isinstance(chat, AnthropicChat)

def test_build_chat_openrouter():
    chat = build_chat("openrouter", api_key="test-key")
    assert isinstance(chat, OpenRouterChat)

def test_build_chat_ollama():
    chat = build_chat("ollama")
    assert isinstance(chat, OllamaChat)

def test_build_chat_unknown_raises():
    with pytest.raises(PhosonLLMError, match="Unknown provider"):
        build_chat("nonexistent_provider")
