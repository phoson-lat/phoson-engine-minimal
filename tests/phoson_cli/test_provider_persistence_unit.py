from pathlib import Path

from phoson_cli.config import PhosonConfig, save_config


def test_save_config_persists_provider_and_model(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    config = PhosonConfig(
        provider="openai",
        model="gpt-4.1-mini",
        subagent_model="gpt-4.1-mini",
        openai_api_key="sk-openai-test",
        sessions_dir=Path("~/.phoson/sessions").expanduser(),
    )

    path = save_config(config)
    text = path.read_text(encoding="utf-8")

    assert 'provider = "openai"' in text
    assert 'model = "gpt-4.1-mini"' in text
