from pathlib import Path

from phoson_cli.config import PhosonConfig, load_config, save_config
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
