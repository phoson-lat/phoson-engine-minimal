from phoson_llm.chats.azure import AzureChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat


def test_is_openai_compatible_subclass():
    chat = AzureChat(
        azure_endpoint="https://my-resource.openai.azure.com",
        api_key="test-key",
        deployment="gpt-4o",
        api_version="2024-08-01-preview",
    )
    assert isinstance(chat, OpenAICompatibleChat)

def test_builds_correct_base_url():
    chat = AzureChat(
        azure_endpoint="https://my-resource.openai.azure.com",
        api_key="test-key",
        deployment="gpt-4o",
        api_version="2024-08-01-preview",
    )
    expected = ("https://my-resource.openai.azure.com/openai/deployments/"
                "gpt-4o/chat/completions?api-version=2024-08-01-preview")
    assert chat._base_url == expected

def test_uses_api_key():
    chat = AzureChat(
        azure_endpoint="https://my-resource.openai.azure.com",
        api_key="az-key-123",
        deployment="gpt-4o",
        api_version="2024-08-01-preview",
    )
    assert chat._client.api_key == "az-key-123"

def test_falls_back_to_env_vars(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env-resource.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key")
    chat = AzureChat()
    assert "env-resource" in chat._base_url
    assert "gpt-4o-mini" in chat._base_url
    assert chat._client.api_key == "env-key"
