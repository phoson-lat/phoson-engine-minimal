"""Real persistence/setup/rebuild paths for the config review regressions."""

import os
import tomllib
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from phoson_cli.theme import DARK, LIGHT, resolve_runtime_theme, default_theme_registry
from phoson_cli.config import PhosonConfig, load_config, save_config
from phoson_cli.__main__ import CliOptions, _apply_overrides, _prepare_cli_theme
from phoson_cli.commands import Command, CommandHandler
from phoson_cli.installer import SetupWizard


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("PHOSON_") or key in {"NO_COLOR", "CLICOLOR"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "phoson_agent.plugins.context_window.ContextWindowResolver._resolve_ollama",
        AsyncMock(return_value=128000),
    )
    return tmp_path


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("source", ["cli", "env", "model-only-cli", "model-only-env"])
@pytest.mark.parametrize("fields", [{"theme"}, None])
def test_unrelated_save_keeps_durable_pair(
    isolated_home, monkeypatch, existing, source, fields
):
    if existing:
        save_config(
            PhosonConfig(
                provider="openai", model="gpt-4o", enabled_providers=["openai"]
            )
        )
    baseline = load_config()
    if source.endswith("env"):
        monkeypatch.setenv("PHOSON_MODEL", "llama3.2")
        if source == "env":
            monkeypatch.setenv("PHOSON_PROVIDER", "ollama")
    config = load_config()
    if source.endswith("cli"):
        _apply_overrides(
            config,
            CliOptions(
                model="llama3.2", provider="ollama" if source == "cli" else None
            ),
        )
    runtime_pair = (config.provider, config.model)
    config = replace(config)  # setup copies must preserve persistence provenance
    config.theme = "light"
    path = save_config(config, only_fields=fields)
    data = tomllib.loads(path.read_text())["defaults"]
    assert (data["provider"], data["model"]) == (baseline.provider, baseline.model)
    assert (config.provider, config.model) == runtime_pair
    monkeypatch.delenv("PHOSON_PROVIDER", raising=False)
    monkeypatch.delenv("PHOSON_MODEL", raising=False)
    reloaded = load_config()
    assert (reloaded.provider, reloaded.model) == (baseline.provider, baseline.model)
    assert reloaded.theme == "light"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("source", ["cli", "env"])
def test_model_only_save_cannot_write_under_a_different_durable_provider(
    isolated_home, monkeypatch, existing, source
):
    if existing:
        save_config(
            PhosonConfig(
                provider="openai", model="gpt-4o", enabled_providers=["openai"]
            )
        )
    baseline = load_config()
    if source == "env":
        monkeypatch.setenv("PHOSON_PROVIDER", "ollama")
        monkeypatch.setenv("PHOSON_MODEL", "llama3.2")
    config = load_config()
    if source == "cli":
        _apply_overrides(config, CliOptions(provider="ollama", model="llama3.2"))
    path = save_config(config, only_fields={"model"})
    defaults = tomllib.loads(path.read_text())["defaults"]
    assert (defaults["provider"], defaults["model"]) == (
        baseline.provider,
        baseline.model,
    )
    assert (config.provider, config.model) == ("ollama", "llama3.2")


@pytest.mark.asyncio
async def test_first_theme_command_does_not_save_cli_provider_model(isolated_home):
    baseline = load_config()
    config = load_config()
    _apply_overrides(config, CliOptions(provider="ollama", model="llama3.2"))
    repl = SimpleNamespace(
        config=config, theme=DARK, theme_registry=default_theme_registry()
    )
    host = MagicMock()
    await CommandHandler(repl, host=host).handle(Command(name="/theme", args="light"))
    host.apply_theme.assert_called_once_with(LIGHT)
    reloaded = load_config()
    assert (reloaded.provider, reloaded.model) == (baseline.provider, baseline.model)
    assert reloaded.theme == "light"
    assert (config.provider, config.model) == ("ollama", "llama3.2")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cli", "env"])
async def test_setup_accepts_transient_provider_and_saves_only_selected_pair(
    isolated_home, monkeypatch, source
):
    save_config(
        PhosonConfig(provider="openai", model="gpt-4o", enabled_providers=["openai"])
    )
    if source == "env":
        monkeypatch.setenv("PHOSON_PROVIDER", "anthropic")
    config = load_config()
    if source == "cli":
        _apply_overrides(config, CliOptions(provider="anthropic"))
    wizard = SetupWizard(config)
    assert wizard.enabled_providers == ["openai"]
    # Keep the durable enabled list and run the real wizard/save path.
    wizard.session.prompt_async = AsyncMock(
        side_effect=[
            "",
            "setup-key",
            "openai",
            "gpt-4.1",
            "gpt-4.1-mini",
            str(isolated_home / "sessions"),
            "10",
            "y",
            "light",
            "y",
        ]
    )
    monkeypatch.setattr(
        "phoson_cli.installer.list_available_models", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "phoson_cli.terminal_theme.detect_terminal_theme", lambda: False
    )
    updated = await wizard.run()
    monkeypatch.delenv("PHOSON_PROVIDER", raising=False)
    reloaded = load_config()
    assert (updated.provider, updated.model) == ("openai", "gpt-4.1")
    assert (reloaded.provider, reloaded.model) == ("openai", "gpt-4.1")
    assert reloaded.enabled_providers == ["openai"]
    assert (config.provider, config.model) == ("anthropic", "gpt-4o")


@pytest.mark.asyncio
@pytest.mark.parametrize("frontend", ["classic", "fullscreen"])
@pytest.mark.parametrize("via_picker", [False, True])
async def test_theme_command_survives_real_engine_rebuild(
    isolated_home, monkeypatch, frontend, via_picker
):
    from phoson_cli.repl import PhosonRepl
    from phoson_cli.fullscreen.app import PhosonApp

    monkeypatch.setattr(
        "phoson_cli.controller.build_chat",
        lambda config: MagicMock(aclose=AsyncMock()),
    )
    config = PhosonConfig(provider="ollama", sessions_dir=isolated_home / "sessions")
    _apply_overrides(config, CliOptions(theme="dark"))
    _prepare_cli_theme(config)
    if frontend == "fullscreen":
        app = PhosonApp(config)
        repl, handler = app.repl, app._commands
    else:
        repl = PhosonRepl(config)
        handler = CommandHandler(repl)
    try:
        assert repl.theme is DARK
        if via_picker:
            from phoson_cli.theme_picker import ThemePickerResult

            monkeypatch.setattr(handler, "_picker_unavailable", lambda usage: False)
            monkeypatch.setattr(
                handler.host,
                "pick_theme",
                AsyncMock(return_value=ThemePickerResult(theme_name="light")),
            )
        await handler.handle(Command(name="/theme", args="" if via_picker else "light"))
        assert repl.theme is LIGHT
        old_engine = repl.engine
        old_plugin_ui = repl._controller.plugin_ui
        await repl.set_model("llama3.2")
        assert repl.engine is not old_engine
        assert repl._controller.plugin_ui is not old_plugin_ui
        assert repl._controller.plugin_ui.theme is LIGHT
        assert resolve_runtime_theme(config, repl.theme_registry) is LIGHT
        assert load_config().theme == "light"
    finally:
        await repl.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cli", "env"])
async def test_explicit_model_selection_persists_transient_route(
    isolated_home, monkeypatch, source
):
    from phoson_cli.repl import PhosonRepl

    save_config(
        PhosonConfig(provider="openai", model="gpt-4o", enabled_providers=["openai"])
    )
    if source == "env":
        monkeypatch.setenv("PHOSON_PROVIDER", "ollama")
    config = load_config()
    if source == "cli":
        _apply_overrides(config, CliOptions(provider="ollama", model="llama3.2"))
    monkeypatch.setattr(
        "phoson_cli.controller.build_chat", lambda config: MagicMock(aclose=AsyncMock())
    )
    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers", AsyncMock(return_value=[])
    )
    repl = PhosonRepl(config)
    try:
        await CommandHandler(repl).handle(Command(name="/model", args="llama3.3"))
        monkeypatch.delenv("PHOSON_PROVIDER", raising=False)
        reloaded = load_config()
        assert (reloaded.provider, reloaded.model) == ("ollama", "llama3.3")
        assert reloaded.enabled_providers == ["openai", "ollama"]
        config.theme = "light"
        save_config(config, only_fields={"theme"})
        assert load_config().model == "llama3.3"
    finally:
        await repl.shutdown()


def test_cli_theme_still_wins_until_interactive_change(isolated_home, monkeypatch):
    monkeypatch.setenv("PHOSON_THEME", "ansi")
    config = load_config()
    _apply_overrides(config, CliOptions(theme="dark"))
    _prepare_cli_theme(config)
    registry = default_theme_registry()
    assert resolve_runtime_theme(config, registry) is DARK
    assert resolve_runtime_theme(config, registry) is DARK
    config.theme = "light"
    assert resolve_runtime_theme(config, registry) is LIGHT
    assert resolve_runtime_theme(config, default_theme_registry()) is LIGHT
