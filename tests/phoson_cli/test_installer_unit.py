from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from phoson_cli.config import PhosonConfig, load_config, save_config
from phoson_cli.labels import PLUGIN_LABELS
from phoson_cli.installer import SetupWizard


def test_save_config_persists_api_keys(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    config = PhosonConfig(
        provider="openrouter",
        model="openai/gpt-4.1-mini",
        subagent_model="openai/gpt-4.1-mini",
        openrouter_api_key="sk-or-test",
        openai_api_key="sk-openai-test",
        sessions_dir=Path("~/.phoson/sessions").expanduser(),
    )

    path = save_config(config)
    text = path.read_text(encoding="utf-8")

    assert 'openrouter_api_key = "sk-or-test"' in text
    assert 'openai_api_key = "sk-openai-test"' in text
    assert 'enabled_providers = "openrouter,openai"' in text
    assert 'provider = "openrouter"' in text


def test_load_config_reads_api_keys_from_file(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "openai"
model = "gpt-4.1-mini"
openai_api_key = "sk-test"
anthropic_api_key = "anth-test"
openrouter_api_key = "sk-or-test"
ollama_base_url = "http://localhost:11434"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    config = load_config()

    assert config.openai_api_key == "sk-test"
    assert config.anthropic_api_key == "anth-test"
    assert config.openrouter_api_key == "sk-or-test"
    assert config.ollama_base_url == "http://localhost:11434"


def test_setup_wizard_masks_secret() -> None:
    wizard = SetupWizard()

    assert wizard._mask_secret("sk-1234567890") == "sk-1•••••7890"
    assert wizard._mask_secret(None) == "—"


@pytest.mark.asyncio
async def test_pick_plugins_toggles_and_keeps_order(monkeypatch) -> None:
    wizard = SetupWizard(PhosonConfig())
    monkeypatch.setattr(wizard, "_prompt_text", AsyncMock(side_effect=["1 3", ""]))

    config = await wizard._pick_plugins(PhosonConfig())

    assert config.plugins == ["phoson-plugin-monitor", "phoson-plugin-mcp"]


@pytest.mark.asyncio
async def test_pick_plugins_toggle_twice_deselects(monkeypatch) -> None:
    wizard = SetupWizard(PhosonConfig())
    monkeypatch.setattr(wizard, "_prompt_text", AsyncMock(side_effect=["1", "1", ""]))

    config = await wizard._pick_plugins(PhosonConfig())

    assert config.plugins == []


@pytest.mark.asyncio
async def test_pick_plugins_preserves_unknown_specs(monkeypatch) -> None:
    wizard = SetupWizard(PhosonConfig())
    custom = {"name": "path:/tmp/my_plugin.py", "config": {"k": 1}}
    monkeypatch.setattr(wizard, "_prompt_text", AsyncMock(side_effect=["2", ""]))

    config = await wizard._pick_plugins(PhosonConfig(plugins=[custom]))

    assert config.plugins == [custom, "phoson-plugin-bgjobs"]


@pytest.mark.asyncio
async def test_pick_plugins_prefills_existing_selection(monkeypatch) -> None:
    initial = PhosonConfig(plugins=["phoson-plugin-ssh"])
    wizard = SetupWizard(initial)
    monkeypatch.setattr(wizard, "_prompt_text", AsyncMock(side_effect=[""]))

    config = await wizard._pick_plugins(initial)

    assert config.plugins == ["phoson-plugin-ssh"]


@pytest.mark.asyncio
async def test_pick_plugins_ignores_invalid_tokens(monkeypatch) -> None:
    wizard = SetupWizard(PhosonConfig())
    monkeypatch.setattr(
        wizard,
        "_prompt_text",
        AsyncMock(side_effect=["abc 99", ""]),
    )

    config = await wizard._pick_plugins(PhosonConfig())

    assert config.plugins == []


@pytest.mark.asyncio
async def test_pick_plugins_empty_round_trip_covers_all_specs(monkeypatch) -> None:
    """Every label spec must round-trip through config validation."""
    from phoson_cli.config import _validate_plugin_specs

    validated = _validate_plugin_specs(list(PLUGIN_LABELS), "plugins")
    assert validated == list(PLUGIN_LABELS)


@pytest.mark.asyncio
async def test_text_prompts_reset_session_password_flag(monkeypatch) -> None:
    """Regression: ``is_password`` is stateful on PromptSession.

    A secret prompt sets it to ``True``; plain text prompts must pass
    ``is_password=False`` explicitly, otherwise every later prompt masks
    the user's typed input as ``*``.
    """
    wizard = SetupWizard(PhosonConfig(openai_api_key="k" * 12))
    prompt_async = AsyncMock(side_effect=["", ""])
    monkeypatch.setattr(wizard.session, "prompt_async", prompt_async)

    await wizard._secret_prompt("OpenAI API key", None, field_name="openai_api_key")
    assert prompt_async.call_args_list[0].kwargs["is_password"] is True

    await wizard._prompt_text("Default model", "gpt-4.1-mini")
    assert prompt_async.call_args_list[1].kwargs["is_password"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
async def test_wizard_interrupt_cancels_cleanly(monkeypatch, capsys, interrupt) -> None:
    """Ctrl+C / Ctrl+D abort the wizard without a traceback.

    prompt_toolkit raises KeyboardInterrupt for Ctrl+C and EOFError for
    Ctrl+D; the wizard must convert both into ``SetupCancelled`` with a
    friendly notice and leave the original config untouched.
    """
    from phoson_cli.installer import SetupCancelled

    config = PhosonConfig(provider="openai", model="gpt-4.1-mini")
    wizard = SetupWizard(config)
    monkeypatch.setattr(
        wizard.session, "prompt_async", AsyncMock(side_effect=interrupt())
    )

    with pytest.raises(SetupCancelled):
        await wizard.run()

    out = capsys.readouterr().out
    assert "Setup cancelled" in out
    assert "nothing was saved" in out
    # The wizard's own config was never mutated by the aborted run.
    assert wizard.config is config
    assert wizard.config.model == "gpt-4.1-mini"
