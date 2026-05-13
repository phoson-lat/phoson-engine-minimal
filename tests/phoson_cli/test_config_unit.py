from phoson_cli.config import load_config


def test_load_config_default_subagent_model(monkeypatch, tmp_path) -> None:
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


def test_load_config_file_subagent_model(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[defaults]\nsubagent_model = "openai/gpt-4.1-mini"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SUBAGENT_MODEL", raising=False)

    config = load_config()

    assert config.subagent_model == "openai/gpt-4.1-mini"


def test_load_config_env_subagent_model(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[defaults]\nsubagent_model = "openai/gpt-4.1-mini"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_SUBAGENT_MODEL", "anthropic/claude-3.5-haiku")

    config = load_config()

    assert config.subagent_model == "anthropic/claude-3.5-haiku"


def test_save_config_safely_handles_missing_attributes(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import PhosonConfig, save_config

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    # Create a "minimal" config object that might be missing attributes
    # if it were an old version of the class or a mock.
    class LegacyConfig:
        provider = "openai"
        model = "gpt-4"
        openai_api_key = "sk-..."

    # save_config uses getattr(config, "field", None) so it should handle this
    path = save_config(LegacyConfig())  # type: ignore

    content = path.read_text()
    assert 'provider = "openai"' in content
    assert 'model = "gpt-4"' in content
    assert 'openai_api_key = "sk-..."' in content
    assert "gemini_api_key" not in content  # Should be skipped as it returns None

