"""I-89 — ``/model`` must persist the provider alongside the model.

Covers:
- the pure ``model_provider_for`` helper (prefix inference, router
  exception, picker option authority, aliases);
- ``SessionController.set_model(model, provider=...)``;
- the ``/model`` command persisting a consistent ``(provider, model)``
  pair, refusing to save a pair whose provider has no credentials, and
  leaving the provider untouched when the model stays within a router.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.theme import NO_COLOR
from phoson_cli.config import PhosonConfig, load_config, save_config
from phoson_cli.models import model_provider_for, normalize_provider
from phoson_cli.commands import Command, CommandHandler

# ── model_provider_for (pure helper) ─────────────────────────────────────────


def test_model_provider_for_prefers_option_provider() -> None:
    assert (
        model_provider_for("openai/gpt-4o", "anthropic", option_provider="openai")
        == "openai"
    )
    # The picker's provider field wins even over the id prefix.
    assert (
        model_provider_for("openai/gpt-4o", "anthropic", option_provider="groq")
        == "groq"
    )


def test_model_provider_for_custom_option_falls_back_to_prefix() -> None:
    # "custom" is the fallback tag for the current-model entry — it must
    # not be treated as a real provider.
    assert (
        model_provider_for("openai/gpt-4o", "anthropic", option_provider="custom")
        == "openai"
    )
    assert model_provider_for("gpt-4o", "anthropic", option_provider="custom") is None


def test_model_provider_for_uses_vendor_prefix() -> None:
    assert model_provider_for("openai/gpt-4o", "anthropic") == "openai"
    assert model_provider_for("anthropic/claude-sonnet-4-6", "openai") == "anthropic"
    # Same provider → no switch.
    assert model_provider_for("openai/gpt-4o", "openai") is None


def test_model_provider_for_router_prefix_does_not_switch() -> None:
    # Routers serve other vendors' ids (openai/gpt-4o via OpenRouter).
    assert model_provider_for("openai/gpt-4o", "openrouter") is None
    assert model_provider_for("anthropic/claude-sonnet-4-6", "github") is None


def test_model_provider_for_unknown_prefix_never_switches() -> None:
    # Local/unknown vendor prefixes must never trigger a provider switch.
    assert model_provider_for("qwen/qwen3.6-plus", "anthropic") is None
    assert model_provider_for("my-local-model", "ollama") is None
    assert model_provider_for("gpt-4o", "openai") is None


def test_model_provider_for_normalizes_aliases() -> None:
    assert model_provider_for("google/gemini-2.5-flash", "openai") == "gemini"
    assert model_provider_for("openai/gpt-4o", "Google") == "openai"
    assert normalize_provider("AWS") == "bedrock"
    assert normalize_provider("Grok") == "xai"


# ── SessionController.set_model(model, provider=...) ─────────────────────────


class _Sink:
    on_subagent_progress = None

    def set_session(self, session_id: str) -> None:
        self.session_id = session_id


async def test_controller_set_model_switches_provider(tmp_path) -> None:
    from phoson_cli.controller import SessionController

    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path,
    )
    fake_engine = SimpleNamespace(context=SimpleNamespace(extra={}), tools=[])
    with (
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.AgentEngine", return_value=fake_engine),
        patch(
            "phoson_cli.controller.SessionController._refresh_context_window",
            new=AsyncMock(),
        ),
    ):
        controller = SessionController(config, _Sink())
        await controller.set_model("gpt-4o", provider="openai")

        assert controller.config.provider == "openai"
        assert controller.config.model == "gpt-4o"
        assert controller.current_model == "gpt-4o"

        # Same provider (alias included) → no provider mutation.
        await controller.set_model("gpt-4.1-mini", provider="OpenAI")
        assert controller.config.provider == "openai"


# ── /model command: persistence ──────────────────────────────────────────────


class _DummyRenderer:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class _DummyRepl:
    """Command-handler test double that records set_model calls."""

    def __init__(self, config: PhosonConfig) -> None:
        self.config = config
        self.current_model = config.model
        self.subagent_model = config.subagent_model or config.model
        self.theme = NO_COLOR
        self.renderer = _DummyRenderer()
        self.engine = SimpleNamespace(context=SimpleNamespace(extra={}))
        self.model_calls: list[tuple[str, str | None]] = []

    async def set_model(self, model: str, provider: str | None = None) -> None:
        self.model_calls.append((model, provider))
        if provider is not None:
            self.config.provider = provider
        self.current_model = model
        self.config.model = model


def _handler(repl: _DummyRepl) -> CommandHandler:
    return CommandHandler(repl)


@pytest.mark.asyncio
async def test_model_command_persists_provider_and_model(tmp_path, monkeypatch) -> None:
    """Explicit /model <id> of another provider switches + saves both."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    result = await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    assert result is True
    assert repl.model_calls == [("openai/gpt-4o", "openai")]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in text
    assert 'model = "openai/gpt-4o"' in text

    # Acceptance criterion: a restart reproduces the same (provider, model).
    reloaded = load_config()
    assert reloaded.provider == "openai"
    assert reloaded.model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_model_command_refuses_unconfigured_provider(
    tmp_path, monkeypatch
) -> None:
    """A model whose provider has no credentials must not be saved."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    result = await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    assert result is True
    assert repl.model_calls == []  # runtime untouched
    assert any("no credentials" in e for e in repl.renderer.errors)
    assert not (tmp_path / ".phoson" / "config.toml").exists()


@pytest.mark.asyncio
async def test_model_command_router_prefix_keeps_provider(
    tmp_path, monkeypatch
) -> None:
    """With a router active, vendor-prefixed ids do not switch provider."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="openrouter",
        model="qwen/qwen3.6-plus",
        openrouter_api_key="sk-or-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    result = await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    assert result is True
    assert repl.model_calls == [("openai/gpt-4o", None)]
    assert config.provider == "openrouter"
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openrouter"' in text
    assert 'model = "openai/gpt-4o"' in text


@pytest.mark.asyncio
async def test_model_command_picker_option_provider_is_authoritative(
    tmp_path, monkeypatch
) -> None:
    """Picker selection: the option's provider field drives the switch."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        groq_api_key="gsk-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    async def fake_list_available_models(cfg):
        return [
            SimpleNamespace(id="llama-3.3-70b", provider="groq"),
            SimpleNamespace(id="claude-sonnet-4-6", provider="anthropic"),
        ]

    async def fake_pick_model(models, current_model, theme=None):
        return SimpleNamespace(model_id="llama-3.3-70b", cancelled=False)

    monkeypatch.setattr(
        "phoson_cli.commands.list_available_models", fake_list_available_models
    )
    monkeypatch.setattr("phoson_cli.commands.pick_model", fake_pick_model)

    result = await _handler(repl).handle(Command(name="/model", args=""))

    assert result is True
    assert repl.model_calls == [("llama-3.3-70b", "groq")]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "groq"' in text
    assert 'model = "llama-3.3-70b"' in text


@pytest.mark.asyncio
async def test_model_command_same_provider_saves_model_only(
    tmp_path, monkeypatch
) -> None:
    """No provider change → provider line left exactly as it was."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="openai",
        model="gpt-4o",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    # Pre-existing file with a provider line the save must not rewrite.
    save_config(config)
    repl = _DummyRepl(config)

    result = await _handler(repl).handle(Command(name="/model", args="gpt-4.1-mini"))

    assert result is True
    assert repl.model_calls == [("gpt-4.1-mini", None)]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in text
    assert 'model = "gpt-4.1-mini"' in text


@pytest.mark.asyncio
async def test_model_command_env_provider_becomes_self_containing_file(
    tmp_path, monkeypatch
) -> None:
    """Issue point 2: provider from env → the saved file must carry it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PHOSON_PROVIDER", "anthropic")
    config = PhosonConfig(
        provider="anthropic",  # resolved from env at load time
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in text
    assert 'model = "openai/gpt-4o"' in text

    # Next launch WITHOUT the env var must reproduce the selection.
    monkeypatch.delenv("PHOSON_PROVIDER")
    reloaded = load_config()
    assert reloaded.provider == "openai"
    assert reloaded.model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_provider_command_refreshes_enabled_providers(
    tmp_path, monkeypatch
) -> None:
    """Narrow /provider save keeps enabled_providers consistent (issue pt 3)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="openai",
        model="gpt-4o",
        openai_api_key="sk-openai-test",
        anthropic_api_key="sk-ant-test",
        sessions_dir=tmp_path / "sessions",
    )
    save_config(config)
    repl = _DummyRepl(config)

    async def fake_set_provider(provider: str) -> None:
        config.provider = provider

    repl.set_provider = fake_set_provider  # type: ignore[method-assign]

    result = await _handler(repl).handle(Command(name="/provider", args="anthropic"))

    assert result is True
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "anthropic"' in text
    # Both credentials present → both listed, in the file, in sync.
    assert 'enabled_providers = "openai,anthropic"' in text
