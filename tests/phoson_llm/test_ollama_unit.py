from phoson_cli.config import PhosonConfig, build_chat
from phoson_llm.schemas import Message, ToolDefinition
from phoson_llm.chats.ollama import OllamaChat, _convert_tools, _convert_messages


def test_build_chat_returns_ollama_chat_for_ollama_provider() -> None:
    chat = build_chat(
        PhosonConfig(
            provider="ollama",
            model="llama3",
        )
    )

    assert isinstance(chat, OllamaChat)


def test_convert_messages_strips_system() -> None:
    messages = [
        Message(role="system", content="You are a helpful assistant"),
        Message(role="user", content="Hello"),
    ]

    result = _convert_messages(messages)

    assert all(m.get("role") != "system" for m in result)
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "Hello"


def test_convert_tools_formats_correctly() -> None:
    tools = [
        ToolDefinition(
            name="get_weather",
            description="Get weather for a location",
            parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        )
    ]

    result = _convert_tools(tools)

    assert len(result) == 1
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "get_weather"
    assert result[0]["function"]["description"] == "Get weather for a location"


def test_ollama_chat_default_base_url() -> None:
    chat = OllamaChat()

    assert chat._base_url == "http://localhost:11434"


def test_ollama_chat_custom_base_url() -> None:
    chat = OllamaChat(base_url="http://192.168.1.100:11434")

    assert chat._base_url == "http://192.168.1.100:11434"
