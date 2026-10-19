"""Unit tests for the ``google-code-assist`` provider wiring in the CLI.

Everything here runs offline: the credential store is redirected with
``PHOSON_GOOGLE_AUTH_FILE`` into ``tmp_path`` and every network entry point is
monkeypatched, so no request is ever made and the real
``~/.phoson/auth-google.json`` is untouched.
"""

import io
import time
import asyncio

import pytest
from rich.console import Console

from phoson_cli.config import (
    PhosonConfig,
    _build_chat_adapter,
    has_configured_provider,
    enabled_providers_from_config,
)
from phoson_cli.labels import provider_label
from phoson_cli.models import KNOWN_PROVIDERS, ROUTER_PROVIDERS
from phoson_cli.commands import _AUTH_USAGE, Command, CommandHandler
from phoson_cli.model_selector import ModelListingError, _list_google_code_assist_models
from phoson_llm.auth.code_assist import (
    CODE_ASSIST_BASE_URL,
    CodeAssistTokens,
    save_tokens,
)
from phoson_llm.chats.code_assist import CodeAssistChat, CodeAssistModel

# ─── Helpers ─────────────────────────────────────────────────────────────────


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
    renderer = handler.repl.renderer
    return "\n".join(renderer.info + renderer.warn + renderer.error)


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    """Point the credential store at a throwaway file; return its path."""
    path = tmp_path / "auth-google.json"
    monkeypatch.setenv("PHOSON_GOOGLE_AUTH_FILE", str(path))
    # Also isolate ``~/.phoson/models.json`` lookups (base_url overrides).
    monkeypatch.setenv("HOME", str(tmp_path))
    return path


def _store_login(path, *, email="user@example.com", tier="standard-tier"):
    tokens = CodeAssistTokens(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expiry=time.time() + 3600,
        email=email,
        tier=tier,
        project_id="proj-1234",
    )
    save_tokens(tokens, path)
    return tokens


# ─── Provider catalogs ───────────────────────────────────────────────────────


def test_google_code_assist_is_known_but_not_a_router() -> None:
    assert "google-code-assist" in KNOWN_PROVIDERS
    assert "google-code-assist" not in ROUTER_PROVIDERS


def test_build_chat_adapter_google_code_assist_uses_default_base_url(
    auth_file,
) -> None:
    chat = _build_chat_adapter(
        PhosonConfig(provider="google-code-assist", model="gemini-2.5-pro")
    )

    assert isinstance(chat, CodeAssistChat)
    assert chat.base_url == CODE_ASSIST_BASE_URL


def test_provider_label_google_code_assist() -> None:
    assert provider_label("google-code-assist") == "Google Code Assist"


# ─── has_configured_provider / enabled providers ─────────────────────────────


def test_has_configured_provider_google_code_assist_false_without_login(
    auth_file,
) -> None:
    assert has_configured_provider(PhosonConfig(provider="google-code-assist")) is False


def test_has_configured_provider_google_code_assist_true_after_login(
    auth_file,
) -> None:
    _store_login(auth_file)
    assert has_configured_provider(PhosonConfig(provider="google-code-assist")) is True


def test_enabled_providers_lists_google_code_assist_once_logged_in(auth_file) -> None:
    config = PhosonConfig(provider="ollama")
    assert "google-code-assist" not in enabled_providers_from_config(config)

    _store_login(auth_file)

    assert "google-code-assist" in enabled_providers_from_config(config)


# ─── Model listing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_google_code_assist_models_maps_slugs_and_provider(
    auth_file, monkeypatch
) -> None:
    _store_login(auth_file)

    async def fake_list_models(*, base_url: str) -> list[CodeAssistModel]:
        assert base_url == CODE_ASSIST_BASE_URL
        return [
            CodeAssistModel(slug="gemini-2.5-pro", display_name="Gemini 2.5 Pro"),
            CodeAssistModel(slug="gemini-2.5-flash", display_name="Gemini 2.5 Flash"),
        ]

    monkeypatch.setattr("phoson_llm.chats.code_assist.list_models", fake_list_models)

    config = PhosonConfig(provider="google-code-assist", model="gemini-2.5-flash")
    options = await _list_google_code_assist_models(config)

    assert [option.provider for option in options] == [
        "google-code-assist",
        "google-code-assist",
    ]
    # The current model is prioritised to the front by _prioritize_current.
    assert options[0].id == "gemini-2.5-flash"
    assert {option.id for option in options} == {"gemini-2.5-pro", "gemini-2.5-flash"}
    assert options[0].label == "Gemini 2.5 Flash"


@pytest.mark.asyncio
async def test_list_google_code_assist_models_without_login_raises_hint(
    auth_file, monkeypatch
) -> None:
    async def tripwire(*args, **kwargs):
        raise AssertionError("must not fetch models without a login")

    monkeypatch.setattr("phoson_llm.chats.code_assist.list_models", tripwire)

    with pytest.raises(ModelListingError, match=r"/auth login google-code-assist"):
        await _list_google_code_assist_models(
            PhosonConfig(provider="google-code-assist", model="gemini-2.5-pro")
        )


# ─── /auth command ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_status_without_login_defaults_to_chatgpt(auth_file) -> None:
    handler = _handler()

    kept = await handler.handle(Command(name="/auth", args="status"))

    assert kept is True
    output = _output(handler)
    assert "Not signed in to ChatGPT." in output
    assert "/auth login chatgpt" in output


@pytest.mark.asyncio
async def test_auth_status_google_code_assist_prints_guidance(auth_file) -> None:
    handler = _handler()

    kept = await handler.handle(Command(name="/auth", args="status google-code-assist"))

    assert kept is True
    output = _output(handler)
    assert "Not signed in to Google Code Assist." in output
    assert "/auth login google-code-assist" in output


@pytest.mark.asyncio
async def test_auth_status_google_code_assist_shows_email_tier_without_tokens(
    auth_file,
) -> None:
    tokens = _store_login(auth_file, email="me@example.com", tier="standard-tier")
    handler = _handler()

    await handler.handle(Command(name="/auth", args="status google-code-assist"))

    output = _output(handler)
    assert "me@example.com" in output
    assert "tier=standard-tier" in output
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
async def test_auth_logout_google_code_assist_calls_clear_tokens(
    auth_file, monkeypatch
) -> None:
    calls = {"count": 0}

    def fake_clear_tokens(*args, **kwargs) -> bool:
        calls["count"] += 1
        return True

    monkeypatch.setattr("phoson_llm.auth.code_assist.clear_tokens", fake_clear_tokens)
    handler = _handler()

    await handler.handle(Command(name="/auth", args="logout google-code-assist"))

    assert calls["count"] == 1
    output = _output(handler)
    assert "credentials removed" in output


@pytest.mark.asyncio
async def test_auth_login_google_code_assist_reports_signed_in_email(
    auth_file, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_login(method: str, **kwargs) -> CodeAssistTokens:
        seen["method"] = method
        seen["kwargs"] = kwargs
        return CodeAssistTokens(
            access_token="a",
            refresh_token="r",
            email="me@example.com",
            tier="standard-tier",
        )

    monkeypatch.setattr("phoson_llm.auth.code_assist.login", fake_login)
    handler = _handler()

    await handler.handle(Command(name="/auth", args="login google-code-assist"))

    assert seen["method"] == "auto"
    assert callable(seen["kwargs"]["on_prompt"])
    output = _output(handler)
    assert "me@example.com" in output
    assert "/provider google-code-assist" in output


@pytest.mark.asyncio
async def test_auth_login_google_code_assist_terminal_error_is_reported_not_raised(
    auth_file, monkeypatch
) -> None:
    from phoson_llm.auth.code_assist import CodeAssistAuthError

    async def fake_login(method: str, **kwargs) -> CodeAssistTokens:
        raise CodeAssistAuthError("login denied", terminal=True)

    monkeypatch.setattr("phoson_llm.auth.code_assist.login", fake_login)
    handler = _handler()

    kept = await handler.handle(Command(name="/auth", args="login google-code-assist"))

    assert kept is True
    assert any("login denied" in message for message in handler.repl.renderer.error)


# ─── Setup wizard ────────────────────────────────────────────────────────────


def _wizard(console: Console | None = None):
    from phoson_cli.installer import SetupWizard

    return SetupWizard(
        PhosonConfig(), console=console or Console(file=io.StringIO(), highlight=False)
    )


def test_setup_wizard_google_code_assist_summary_not_signed_in(auth_file) -> None:
    assert _wizard()._google_code_assist_summary() == "not signed in"


def test_setup_wizard_google_code_assist_summary_shows_email_and_tier(
    auth_file,
) -> None:
    _store_login(auth_file, email="me@example.com", tier="standard-tier")

    assert _wizard()._google_code_assist_summary() == "me@example.com (standard-tier)"


def test_setup_wizard_infer_enabled_providers_preselects_google_code_assist(
    auth_file,
) -> None:
    wizard = _wizard()
    assert "google-code-assist" not in wizard._infer_enabled_providers(PhosonConfig())

    _store_login(auth_file)

    assert "google-code-assist" in wizard._infer_enabled_providers(PhosonConfig())


def test_setup_wizard_subscription_flows_are_provider_specific() -> None:
    """Each subscription provider gets its own flow wording (device vs paste)."""
    wizard = _wizard()
    chatgpt_methods, _, chatgpt_notice = wizard._SUBSCRIPTION_FLOWS["chatgpt"]
    gca_methods, _, gca_notice = wizard._SUBSCRIPTION_FLOWS["google-code-assist"]

    assert chatgpt_methods == ("browser", "device")
    assert gca_methods == ("browser", "headless")
    assert "device code" in chatgpt_notice
    assert "URL" in gca_notice
    # Every accepted spelling folds onto the canonical method of its provider.
    assert wizard._LOGIN_METHOD_ALIASES["chatgpt"]["headless"] == "device"
    assert wizard._LOGIN_METHOD_ALIASES["google-code-assist"]["device"] == "headless"


def test_setup_wizard_lists_subscription_providers_first_and_tags_them() -> None:
    stream = io.StringIO()
    console = Console(file=stream, highlight=False, width=120)
    wizard = _wizard(console)
    wizard.enabled_providers = ["chatgpt"]

    async def _enter(label: str, default: str | None = None) -> str:
        return ""  # accept the pre-selected providers

    wizard._prompt_text = _enter  # type: ignore[method-assign]
    asyncio.run(wizard._pick_enabled_providers())

    out = stream.getvalue()
    assert "1. [x] ChatGPT  · subscription, no API key" in out
    assert "2. [ ] Google Code Assist  · subscription, no API key" in out
    # The API-key providers stay below, so the subscription ones read first.
    assert out.index("ChatGPT") < out.index("OpenRouter")
    assert out.index("Google Code Assist") < out.index("OpenRouter")


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
        provider="google-code-assist",
        options=[
            ModelOption(id=slug, label=slug, provider="google-code-assist")
            for slug in slugs
        ],
    )

    async def fake(config, providers):  # noqa: ARG001
        return [listing]

    monkeypatch.setattr(selector, "list_models_for_providers", fake)


@pytest.mark.asyncio
async def test_provider_google_code_assist_self_enables_when_signed_in(
    auth_file, monkeypatch
) -> None:
    """A stored login must not be blocked by a stale ``enabled_providers``."""
    _store_login(auth_file)
    _fake_catalogue(monkeypatch, ["gemini-2.5-pro", "gemini-2.5-flash"])
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    assert "google-code-assist" not in enabled_providers_from_config(config)
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(
            Command(name="/provider", args="google-code-assist")
        )
        is True
    )

    assert repl.switched == ["google-code-assist"]
    assert "google-code-assist" in config.enabled_providers
    assert "enabling it in config.toml" in _output(handler)
    assert "google-code-assist" in enabled_providers_from_config(config)
    # ``qwen3.8-27b`` is not served by the subscription: a real slug is used.
    assert repl.models == [("gemini-2.5-pro", "google-code-assist")]
    assert "not served by the Google Code Assist subscription" in _output(handler)


@pytest.mark.asyncio
async def test_provider_google_code_assist_keeps_a_served_model(
    auth_file, monkeypatch
) -> None:
    """A model the subscription already serves must not be replaced."""
    _store_login(auth_file)
    _fake_catalogue(monkeypatch, ["gemini-2.5-pro", "gemini-2.5-flash"])
    config = PhosonConfig(
        provider="google-code-assist",
        model="gemini-2.5-pro",
        enabled_providers=["google-code-assist"],
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(
            Command(name="/provider", args="google-code-assist")
        )
        is True
    )

    assert repl.models == []
    assert "not served by the Google Code Assist subscription" not in _output(handler)


@pytest.mark.asyncio
async def test_provider_google_code_assist_without_login_is_refused(
    auth_file, monkeypatch
) -> None:
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(
            Command(name="/provider", args="google-code-assist")
        )
        is True
    )

    assert repl.switched == []
    assert config.enabled_providers == ["alibaba"]
    assert "run /auth login google-code-assist" in _output(handler)


# ─── Provider aliases (code-assist / … → google-code-assist) ─────────────────

_CODE_ASSIST_ALIASES = (
    "code-assist",
    "code_assist",
    "codeassist",
    "gemini-code-assist",
    "gca",
)


def test_alias_registries_agree_on_the_code_assist_aliases() -> None:
    """``config._PROVIDER_ALIASES`` and ``models.PROVIDER_ALIASES`` are two
    separate registries; every alias must fold to the same canonical name."""
    from phoson_cli.config import _PROVIDER_ALIASES, _provider_name
    from phoson_cli.models import PROVIDER_ALIASES, normalize_provider

    assert dict(PROVIDER_ALIASES) == dict(_PROVIDER_ALIASES)
    for alias in _CODE_ASSIST_ALIASES:
        assert PROVIDER_ALIASES[alias] == "google-code-assist"
        assert normalize_provider(alias) == "google-code-assist"
        assert _provider_name(alias) == "google-code-assist"
        assert normalize_provider(f"  {alias.upper()}  ") == "google-code-assist"


def test_google_alias_still_folds_to_gemini() -> None:
    from phoson_cli.config import _provider_name
    from phoson_cli.models import normalize_provider

    assert normalize_provider("google") == "gemini"
    assert _provider_name("  Google ") == "gemini"
    # The new aliases must not shadow the plain Gemini API provider.
    assert normalize_provider("gemini") == "gemini"


def test_has_configured_provider_accepts_a_code_assist_alias(auth_file) -> None:
    assert has_configured_provider(PhosonConfig(provider="gca")) is False
    _store_login(auth_file)
    assert has_configured_provider(PhosonConfig(provider="gca")) is True


@pytest.mark.asyncio
async def test_provider_code_assist_alias_switches_to_google_code_assist(
    auth_file, monkeypatch
) -> None:
    _store_login(auth_file)
    _fake_catalogue(monkeypatch, ["gemini-2.5-pro", "gemini-2.5-flash"])
    config = PhosonConfig(
        provider="alibaba", model="qwen3.8-27b", enabled_providers=["alibaba"]
    )
    handler, repl = _provider_handler(config, monkeypatch)

    assert (
        await handler._cmd_provider(Command(name="/provider", args="code-assist"))
        is True
    )

    assert repl.switched == ["google-code-assist"]
    assert "google-code-assist" in config.enabled_providers
    assert repl.models == [("gemini-2.5-pro", "google-code-assist")]