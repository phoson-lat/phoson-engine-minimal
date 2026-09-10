from phoson_llm.retry import RetryingChat
from phoson_cli.config import PhosonConfig, build_chat, has_configured_provider
from phoson_llm.chats.omniroute import OMNIROUTE_DEFAULT_BASE_URL, OmniRouteChat
from phoson_llm.chats.openai_compatible import OpenAICompatibleChat


def test_omniroute_defaults() -> None:
    chat = OmniRouteChat()
    assert isinstance(chat, OpenAICompatibleChat)
    assert chat._base_url == OMNIROUTE_DEFAULT_BASE_URL
    assert "OmniRoute" in repr(chat)


def test_omniroute_custom_base_url() -> None:
    chat = OmniRouteChat(base_url="http://gateway.example:20128/v1")
    assert chat._base_url == "http://gateway.example:20128/v1"


def test_build_chat_returns_omniroute_chat() -> None:
    chat = build_chat(PhosonConfig(provider="omniroute"))
    assert isinstance(chat, RetryingChat)
    assert isinstance(chat._inner, OmniRouteChat)
    assert chat._inner._base_url == OMNIROUTE_DEFAULT_BASE_URL


def test_build_chat_honours_omniroute_base_url() -> None:
    chat = build_chat(
        PhosonConfig(
            provider="omniroute",
            omniroute_base_url="http://192.168.1.10:20128/v1",
        )
    )
    assert isinstance(chat, RetryingChat)
    assert isinstance(chat._inner, OmniRouteChat)
    assert chat._inner._base_url == "http://192.168.1.10:20128/v1"


def test_omniroute_needs_no_credential() -> None:
    assert has_configured_provider(PhosonConfig(provider="omniroute"))
