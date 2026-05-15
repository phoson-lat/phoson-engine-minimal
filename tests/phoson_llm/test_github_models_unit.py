from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.github_models import GitHubModelsChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

GITHUB_BASE = "https://models.inference.ai.azure.com"


def test_is_openai_compatible_subclass():
    chat = GitHubModelsChat(api_key="test-token")
    assert isinstance(chat, OpenAICompatibleChat)
    assert isinstance(chat, BaseLLMChat)


def test_default_base_url():
    chat = GitHubModelsChat(api_key="test-token")
    assert chat._base_url == GITHUB_BASE


def test_falls_back_to_github_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    chat = GitHubModelsChat()
    assert chat._client.api_key == "env-token"


def test_repr_includes_github_models():
    chat = GitHubModelsChat(api_key="test-token")
    assert "GitHubModels" in repr(chat)
