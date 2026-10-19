"""Credential helpers for providers that authenticate outside an API key.

Currently exposes ChatGPT subscription login (OAuth 2.0 + PKCE) for the
``chatgpt`` provider. Exports are lazy (PEP 562) so importing
:mod:`phoson_llm.auth` never pulls in ``httpx`` unless a symbol is used.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from phoson_llm.auth.chatgpt import (
        DeviceCode as DeviceCode,
    )
    from phoson_llm.auth.chatgpt import (
        ChatGPTTokens as ChatGPTTokens,
    )
    from phoson_llm.auth.chatgpt import (
        ChatGPTAuthError as ChatGPTAuthError,
    )
    from phoson_llm.auth.chatgpt import (
        ChatGPTAuthStatus as ChatGPTAuthStatus,
    )

_MODULES = {
    "chatgpt": "phoson_llm.auth.chatgpt",
}

__all__ = [
    "ChatGPTAuthError",
    "ChatGPTAuthStatus",
    "ChatGPTTokens",
    "DeviceCode",
    "auth_status",
    "clear_tokens",
    "has_tokens",
    "import_codex_tokens",
    "load_tokens",
    "login",
    "save_tokens",
]


def __getattr__(name: str) -> Any:
    """Resolve auth symbols lazily (PEP 562)."""
    if name in _MODULES:
        import importlib

        return importlib.import_module(_MODULES[name])
    if name in __all__:
        from phoson_llm.auth import chatgpt

        return getattr(chatgpt, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
