from pathlib import Path

from phoson_cli.config import load_config


def test_load_config_uses_default_subagent_model_when_not_overridden(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SUBAGENT_MODEL", raising=False)
    monkeypatch.delenv("PHOSON_MODEL", raising=False)
    monkeypatch.delenv("PHOSON_PROVIDER", raising=False)
    monkeypatch.delenv("PHOSON_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("PHOSON_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("PHOSON_SAFE_MODE", raising=False)

    config = load_config()

    assert config.subagent_model == "google/gemini-3.1-flash-lite-preview"


def test_load_config_prefers_file_subagent_model_over_default(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[defaults]\nsubagent_model = \"openai/gpt-4.1-mini\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SUBAGENT_MODEL", raising=False)

    config = load_config()

    assert config.subagent_model == "openai/gpt-4.1-mini"


def test_load_config_prefers_env_subagent_model_over_file(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[defaults]\nsubagent_model = \"openai/gpt-4.1-mini\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_SUBAGENT_MODEL", "anthropic/claude-3.5-haiku")

    config = load_config()

    assert config.subagent_model == "anthropic/claude-3.5-haiku"
