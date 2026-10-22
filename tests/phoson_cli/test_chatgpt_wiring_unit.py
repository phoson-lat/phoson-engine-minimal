"""Unit tests for the ``chatgpt`` provider wiring in the CLI.

Everything here runs offline: the credential store is redirected with
``PHOSON_AUTH_FILE`` into ``tmp_path`` and every network entry point is
monkeypatched, so no request is ever made and the real
``~/.phoson/auth.json`` is untouched.
"""

import io
import json
import time
import base64

import pytest
from rich.console import Console

from phoson_cli.config import (
    PhosonConfig,
    _build_chat_adapter,
    has_configured_provider,
    enabled_providers_from_config,
)
from phoson_cli.models import KNOWN_PROVIDERS, ROUTER_PROVIDERS
from phoson_cli.commands import _AUTH_USAGE, Command, CommandHandler
from phoson_llm.auth.chatgpt import ChatGPTTokens, save_tokens
from phoson_llm.chats.chatgpt import CHATGPT_CODEX_BASE_URL, ChatGPTChat, ChatGPTModel
from phoson_cli.model_selector import ModelListingError, _list_chatgpt_models

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    return f"{_b64url({'alg': 'none', 'typ': 'JWT'})}.{_b64url(claims)}.signature"


class _RecordingRenderer:
    """Renderer stub that captures the three notice channels."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_warn(self, message: str) -> None:
        self.warn.append(message)

    def print_error(self, message: str) -> None:
        self.error.append(message)


class _Repl:
    """Minimal REPL facade: ``CommandHandler`` only touches ``renderer``."""

    def __init__(self) -> None:
        self.renderer = _RecordingRenderer()


def _handler() -> CommandHandler:
    return CommandHandler(_Repl())  # type: ignore[arg-type]


def _output(handler: CommandHandler) -> str:
    renderer = handler.session.renderer
    return "\n".join(renderer.info + renderer.warn + renderer.error)


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    """Point the credential store at a throwaway file; return its path."""
    path = tmp_path / "auth.json"
    monkeypatch.setenv("PHOSON_AUTH_FILE", str(path))
    return path


def _store_login(path, *, email="user@example.com", plan="plus") -> ChatGPTTokens:
    id_token = _jwt(
        {
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": plan,
                "chatgpt_account_id": "acct-1234",
            },
        }
    )
    access = _jwt({"exp": time.time() + 3600})
    tokens = ChatGPTTokens(
        access_token=access,
        refresh_token="refresh-secret",
        id_token=id_token,
        account_id="acct-1234",
    )
    save_tokens(tokens, path)
    return tokens


# ─── Provider catalogs ───────────────────────────────────────────────────────


def test_chatgpt_is_known_but_not_a_router() -> None:
    assert "chatgpt" in KNOWN_PROVIDERS
    assert "chatgpt" not in ROUTER_PROVIDERS


def test_build_chat_adapter_chatgpt_uses_codex_base_url() -> None:
    chat = _build_chat_adapter(PhosonConfig(provider="chatgpt", model="gpt-5-codex"))

    assert isinstance(chat, ChatGPTChat)
    assert chat.base_url == CHATGPT_CODEX_BASE_URL
    assert chat.responses_url == f"{CHATGPT_CODEX_BASE_URL}/responses"


# ─── has_configured_provider / enabled providers ─────────────────────────────


def test_has_configured_provider_chatgpt_false_without_login(auth_file) -> None:
    assert has_configured_provider(PhosonConfig(provider="chatgpt")) is False


def test_has_configured_provider_chatgpt_true_after_login(auth_file) -> None:
    _store_login(auth_file)
    assert has_configured_provider(PhosonConfig(provider="chatgpt")) is True


def test_has_configured_provider_ollama_needs_no_credential(auth_file) -> None:
    assert has_configured_provider(PhosonConfig(provider="ollama")) is True


def test_enabled_providers_lists_chatgpt_once_logged_in(auth_file) -> None:
    config = PhosonConfig(provider="ollama")
    assert "chatgpt" not in enabled_providers_from_config(config)

    _store_login(auth_file)

    assert "chatgpt" in enabled_providers_from_config(config)


# ─── Model listing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_chatgpt_models_maps_slugs_and_provider(
    auth_file, monkeypatch
) -> None:
    _store_login(auth_file)

    async def fake_list_models(*, base_url: str) -> list[ChatGPTModel]:
        assert base_url == CHATGPT_CODEX_BASE_URL
        return [
            ChatGPTModel(slug="gpt-5", display_name="GPT-5"),
            ChatGPTModel(slug="gpt-5-codex", display_name="GPT-5 Codex"),
        ]

    monkeypatch.setattr("phoson_llm.chats.chatgpt.list_models", fake_list_models)

    config = PhosonConfig(provider="chatgpt", model="gpt-5-codex")
    options = await _list_chatgpt_models(config)

    assert [option.provider for option in options] == ["chatgpt", "chatgpt"]
    # The current model is prioritised to the front by _prioritize_current.
    assert options[0].id == "gpt-5-codex"
    assert {option.id for option in options} == {"gpt-5", "gpt-5-codex"}
    assert options[0].label == "GPT-5 Codex"


@pytest.mark.asyncio
async def test_list_chatgpt_models_without_login_raises_hint(
    auth_file, monkeypatch
) -> None:
    async def tripwire(*args, **kwargs):
        raise AssertionError("must not fetch models without a login")

    monkeypatch.setattr("phoson_llm.chats.chatgpt.list_models", tripwire)

    with pytest.raises(ModelListingError, match=r"/auth login chatgpt"):
        await _list_chatgpt_models(PhosonConfig(provider="chatgpt", model="gpt-5"))


# ─── /auth command ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_status_without_login_prints_guidance(auth_file) -> None:
    handler = _handler()

    kept = await handler.handle(Command(name="/auth", args=""))

    assert kept is True
    output = _output(handler)
    assert "Not signed in to ChatGPT." in output
    assert "/auth login chatgpt" in output


@pytest.mark.asyncio
async def test_auth_status_shows_email_plan_and_expiry_without_tokens(
    auth_file,
) -> None:
    tokens = _store_login(auth_file, email="me@example.com", plan="pro")
    handler = _handler()

    await handler.handle(Command(name="/auth", args="status"))

    output = _output(handler)
    assert "me@example.com" in output
    assert "plan=pro" in output
    assert "access token expires" in output
    # The bearer/refresh tokens must never be printed.
    assert tokens.access_token not in output
    assert tokens.refresh_token not in output


@pytest.mark.asyncio
async def test_auth_unknown_subcommand_prints_usage(auth_file) -> None:
    handler = _handler()

    await handler.handle(Command(name="/auth", args="bogus"))

    assert _AUTH_USAGE in _output(handler)


@pytest.mark.asyncio
async def test_auth_logout_calls_clear_tokens(auth_file, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_clear_tokens(*args, **kwargs) -> bool:
        calls["count"] += 1
        return True

    monkeypatch.setattr("phoson_llm.auth.chatgpt.clear_tokens", fake_clear_tokens)
    handler = _handler()

    await handler.handle(Command(name="/auth", args="logout"))

    assert calls["count"] == 1
    output = _output(handler)
    assert "credentials removed" in output


@pytest.mark.asyncio
async def test_auth_login_device_reports_signed_in_email(
    auth_file, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_login(method: str, **kwargs) -> ChatGPTTokens:
        seen["method"] = method
        seen["kwargs"] = kwargs
        return ChatGPTTokens(
            access_token="a",
            refresh_token="r",
            email="me@example.com",
            plan_type="plus",
        )

    monkeypatch.setattr("phoson_llm.auth.chatgpt.login", fake_login)
    handler = _handler()

    await handler.handle(Command(name="/auth", args="login chatgpt --device"))

    assert seen["method"] == "device"
    assert callable(seen["kwargs"]["on_prompt"])
    assert "me@example.com" in _output(handler)


@pytest.mark.asyncio
async def test_auth_login_terminal_error_is_reported_not_raised(
    auth_file, monkeypatch
) -> None:
    from phoson_llm.auth.chatgpt import ChatGPTAuthError

    async def fake_login(method: str, **kwargs) -> ChatGPTTokens:
        raise ChatGPTAuthError("login denied", terminal=True)

    monkeypatch.setattr("phoson_llm.auth.chatgpt.login", fake_login)
    handler = _handler()

    kept = await handler.handle(Command(name="/auth", args="login chatgpt"))

    assert kept is True
    assert any("login denied" in message for message in handler.session.renderer.error)


# ─── Setup wizard ────────────────────────────────────────────────────────────


def _wizard(console: Console | None = None):
    from phoson_cli.installer import SetupWizard

    return SetupWizard(
        PhosonConfig(), console=console or Console(file=io.StringIO(), highlight=False)
    )


def test_setup_wizard_chatgpt_summary_not_signed_in(auth_file) -> None:
    assert _wizard()._chatgpt_summary() == "not signed in"


def test_setup_wizard_chatgpt_summary_shows_email_and_plan(auth_file) -> None:
    _store_login(auth_file, email="me@example.com", plan="plus")

    assert _wizard()._chatgpt_summary() == "me@example.com (plus)"


@pytest.mark.asyncio
async def test_setup_wizard_skips_login_on_non_interactive_console(
    auth_file, monkeypatch
) -> None:
    console = Console(file=io.StringIO(), highlight=False)
    assert console.is_terminal is False
    wizard = _wizard(console)

    async def tripwire(*args, **kwargs):
        raise AssertionError("login must not run off a TTY")

    monkeypatch.setattr("phoson_llm.auth.chatgpt.login", tripwire)

    await wizard._configure_chatgpt_login()

    assert "Skipping the sign-in on a non-interactive terminal" in (
        console.file.getvalue()
    )


# ─── /provider: a login is the credential ────────────────────────────────────


class _ProviderRepl:
    """REPL facade for ``/provider``: a config plus the recorded switches."""

    def __init__(self, config: PhosonConfig) -> None:
        self.renderer = _RecordingRenderer()
        self.config = config
        self.current_model = config.model
        self.switched: list[str] = []
        self.models: list[tuple[str, str | None]] = []

    async def set_provider(self, provider: str) -> None:
        self.switched.append(provider)

    async def set_model(
        self, model: str, provider: str | None = None, *, reuse_engine: bool = False
    ) -> None:
        self.models.append((model, provider))
        self.current_model = model
        self.config.model = model


def _provider_handler(
    config: PhosonConfig, monkeypatch
) -> tuple[CommandHandler, _ProviderRepl]:
    import phoson_cli.commands as commands_module

    repl = _ProviderRepl(config)
    # Never write the caller's config.toml from a unit test.
    monkeypatch.setattr(commands_module, "save_config", lambda *a, **k: None)
    return CommandHandler(repl), repl  # type: ignore[arg-type]


def _fake_catalogue(monkeypatch, slugs: list[str]) -> None:
    """Stub the picker's own lister so ``/provider`` never hits the network."""
    import phoson_cli.model_selector as selector
    from phoson_cli.model_selector import ModelOption, ProviderListing

    listing = ProviderListing(
        provider="chatgpt",
        options=[
            ModelOption(id=slug, label=slug, provider="chatgpt") for slug in slugs
        ],
    )

    async def fake(config, providers):  # noqa: ARG001
        return [listing]

    monkeypatch.setattr(selector, "list_models_for_providers", fake)


@pytest.mark.asyncio
async def test_provider_chatgpt_self_enables_when_signed_in(
    auth_file, monkeypatch
) -> None:
    """A stored login must not be blocked by a stale ``enabled_providers``."""
    _store_login(auth_file)
    _fake_catalogue(monkeypatch, ["gpt-5.6-luna", "codex-auto-review"])
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    assert "chatgpt" not in enabled_providers_from_config(config)
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(Command(name="/provider", args="chatgpt")) is True
    )

    assert repl.switched == ["chatgpt"]
    assert "chatgpt" in config.enabled_providers
    assert "enabling it in config.toml" in _output(handler)
    assert "chatgpt" in enabled_providers_from_config(config)
    # ``qwen3.8-27b`` is not served by the subscription: a real slug is used.
    assert repl.models == [("gpt-5.6-luna", "chatgpt")]
    assert "not served by the ChatGPT subscription" in _output(handler)


@pytest.mark.asyncio
async def test_provider_chatgpt_keeps_a_served_model(auth_file, monkeypatch) -> None:
    """A model the subscription already serves must not be replaced."""
    _store_login(auth_file)
    _fake_catalogue(monkeypatch, ["gpt-5.6-luna", "codex-auto-review"])
    config = PhosonConfig(
        provider="chatgpt", model="gpt-5.6-luna", enabled_providers=["chatgpt"]
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(Command(name="/provider", args="chatgpt")) is True
    )

    assert repl.models == []
    assert "not served by the ChatGPT subscription" not in _output(handler)


@pytest.mark.asyncio
async def test_provider_chatgpt_survives_a_failed_listing(
    auth_file, monkeypatch
) -> None:
    """An offline catalogue warns instead of breaking the switch."""
    import phoson_cli.model_selector as selector

    _store_login(auth_file)

    async def boom(config, providers):  # noqa: ARG001
        raise RuntimeError("offline")

    monkeypatch.setattr(selector, "list_models_for_providers", boom)
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(Command(name="/provider", args="chatgpt")) is True
    )

    assert repl.switched == ["chatgpt"]
    assert "Could not list ChatGPT models" in _output(handler)


@pytest.mark.asyncio
async def test_provider_chatgpt_without_login_is_refused(
    auth_file, monkeypatch
) -> None:
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(Command(name="/provider", args="chatgpt")) is True
    )

    assert repl.switched == []
    assert "chatgpt" not in config.enabled_providers
    assert "run /auth login chatgpt" in _output(handler)


@pytest.mark.asyncio
async def test_provider_chatgpt_ignores_the_picker_custom_entry(
    auth_file, monkeypatch
) -> None:
    """The injected ``custom`` current-model entry is not "served" by ChatGPT."""
    import phoson_cli.model_selector as selector
    from phoson_cli.model_selector import ModelOption, ProviderListing

    _store_login(auth_file)
    listing = ProviderListing(
        provider="chatgpt",
        options=[
            ModelOption(id="qwen3.8-27b", label="qwen3.8-27b", provider="custom"),
            ModelOption(id="gpt-6-luna", label="gpt-6-luna", provider="chatgpt"),
        ],
    )

    async def fake(config, providers):  # noqa: ARG001
        return [listing]

    monkeypatch.setattr(selector, "list_models_for_providers", fake)
    config = PhosonConfig(
        provider="chatgpt", model="qwen3.8-27b", enabled_providers=["chatgpt"]
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(Command(name="/provider", args="chatgpt")) is True
    )

    assert repl.models == [("gpt-6-luna", "chatgpt")]
    assert "using gpt-6-luna" in _output(handler)


# ─── Provider aliases (codex / openai-codex → chatgpt) ───────────────────────

_CODEX_ALIASES = ("codex", "openai-codex", "openai_codex")


def test_alias_registries_agree_on_the_codex_aliases() -> None:
    """``config._PROVIDER_ALIASES`` and ``models.PROVIDER_ALIASES`` are two
    separate registries; every alias must fold to the same canonical name."""
    from phoson_cli.config import _PROVIDER_ALIASES, _provider_name
    from phoson_cli.models import PROVIDER_ALIASES, normalize_provider

    assert dict(PROVIDER_ALIASES) == dict(_PROVIDER_ALIASES)
    for alias in _CODEX_ALIASES:
        assert PROVIDER_ALIASES[alias] == "chatgpt"
        assert normalize_provider(alias) == "chatgpt"
        assert _provider_name(alias) == "chatgpt"
        assert normalize_provider(f"  {alias.upper()}  ") == "chatgpt"


def test_provider_name_passes_unknown_and_custom_names_through() -> None:
    from phoson_cli.config import _provider_name

    assert _provider_name("  ChatGPt ") == "chatgpt"
    assert _provider_name("some-custom-router") == "some-custom-router"


def test_load_config_keeps_a_provider_alias_but_folds_it_for_consumers(
    tmp_path, monkeypatch
) -> None:
    """``load_config`` preserves the spelling (only the enabled list is
    canonicalized), so consumers must fold through ``_provider_name``."""
    from phoson_cli.config import load_config, _provider_name

    monkeypatch.delenv("PHOSON_PROVIDER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".phoson").mkdir()
    (tmp_path / ".phoson" / "config.toml").write_text(
        '[defaults]\nprovider = "openai-codex"\n'
        'model = "gpt-5.6-luna"\nenabled_providers = "openai-codex,google"\n'
    )

    config = load_config()

    assert config.provider == "openai-codex"
    assert _provider_name(config.provider) == "chatgpt"
    assert enabled_providers_from_config(config) == ["chatgpt", "gemini"]
    assert isinstance(_build_chat_adapter(config), ChatGPTChat)


def test_load_config_keeps_an_unknown_provider_verbatim(tmp_path, monkeypatch) -> None:
    from phoson_cli.config import load_config

    monkeypatch.delenv("PHOSON_PROVIDER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".phoson").mkdir()
    (tmp_path / ".phoson" / "config.toml").write_text(
        '[defaults]\nprovider = "my-custom-thing"\n'
    )

    assert load_config().provider == "my-custom-thing"


def test_env_provider_override_accepts_an_alias(tmp_path, monkeypatch) -> None:
    from phoson_cli.config import load_config, _provider_name

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PHOSON_PROVIDER", "openai-codex")

    assert _provider_name(load_config().provider) == "chatgpt"


def test_cli_provider_flag_accepts_the_codex_alias() -> None:
    from phoson_cli.__main__ import CliOptions, _apply_overrides

    config = PhosonConfig(provider="alibaba", model="qwen3.8-27b")
    _apply_overrides(config, CliOptions(provider="openai-codex"))

    assert config.provider == "chatgpt"
    assert isinstance(_build_chat_adapter(config), ChatGPTChat)


def test_has_configured_provider_accepts_the_codex_alias(auth_file) -> None:
    assert has_configured_provider(PhosonConfig(provider="codex")) is False
    _store_login(auth_file)
    assert has_configured_provider(PhosonConfig(provider="codex")) is True


@pytest.mark.asyncio
async def test_provider_codex_alias_switches_to_chatgpt(auth_file, monkeypatch) -> None:
    _store_login(auth_file)
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    handler, repl = _provider_handler(config, monkeypatch)
    _fake_catalogue(monkeypatch, ["gpt-6-luna", "gpt-5.6-terra"])

    assert await handler._cmd_provider(Command(name="/provider", args="codex")) is True

    assert repl.switched == ["chatgpt"]
    assert "chatgpt" in config.enabled_providers
    assert repl.models == [("gpt-6-luna", "chatgpt")]


@pytest.mark.asyncio
async def test_auth_login_accepts_the_codex_alias(auth_file, monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_login(method: str, **kwargs) -> ChatGPTTokens:
        seen["method"] = method
        return ChatGPTTokens(
            access_token="a",
            refresh_token="r",
            email="alias@example.com",
            plan_type="plus",
        )

    monkeypatch.setattr("phoson_llm.auth.chatgpt.login", fake_login)
    handler = _handler()

    await handler.handle(Command(name="/auth", args="login codex --device"))

    assert seen["method"] == "device"
    output = _output(handler)
    assert "alias@example.com" in output
    assert "/provider chatgpt" in output
