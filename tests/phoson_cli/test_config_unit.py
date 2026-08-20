import os

import pytest

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
    from phoson_cli.config import save_config

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


@pytest.mark.skipif(os.name != "posix", reason="chmod semantics are POSIX-only")
def test_save_config_restricts_permissions(monkeypatch, tmp_path) -> None:
    """The config file holds API keys: it must not be world-readable."""
    from phoson_cli.config import save_config

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    class MinimalConfig:
        provider = "openai"
        model = "gpt-4"
        openai_api_key = "sk-secret"

    path = save_config(MinimalConfig())  # type: ignore

    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600, f"config file mode is {oct(mode)}, expected 0o600"
    dir_mode = os.stat(path.parent).st_mode & 0o777
    assert dir_mode == 0o700, f"config dir mode is {oct(dir_mode)}, expected 0o700"


def test_theme_key_round_trip(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import PhosonConfig, load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    original = PhosonConfig(
        provider="ollama", sessions_dir=home / "sessions", theme="light"
    )
    save_config(original)

    content = (home / ".phoson" / "config.toml").read_text()
    assert 'theme = "light"' in content

    loaded = load_config()
    assert loaded.theme == "light"


def test_theme_key_env_override(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import PhosonConfig, load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    save_config(
        PhosonConfig(provider="ollama", sessions_dir=home / "sessions", theme="light")
    )

    monkeypatch.setenv("PHOSON_THEME", "ansi")
    loaded = load_config()
    assert loaded.theme == "ansi"


# ── Sub-agent tuning keys ────────────────────────────────────────────────────


def test_subagent_keys_defaults(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SUBAGENT_MAX_PARALLEL", raising=False)
    monkeypatch.delenv("PHOSON_SUBAGENT_TIMEOUT", raising=False)

    config = load_config()

    assert config.subagent_max_parallel == 4
    assert config.subagent_timeout_seconds == 300.0


def test_subagent_keys_env_override(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_SUBAGENT_MAX_PARALLEL", "8")
    monkeypatch.setenv("PHOSON_SUBAGENT_TIMEOUT", "120.5")

    config = load_config()

    assert config.subagent_max_parallel == 8
    assert config.subagent_timeout_seconds == pytest.approx(120.5)


def test_subagent_keys_invalid_env_falls_back(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_SUBAGENT_MAX_PARALLEL", "not-a-number")
    monkeypatch.setenv("PHOSON_SUBAGENT_TIMEOUT", "also-not")

    with pytest.warns(UserWarning):
        config = load_config()

    assert config.subagent_max_parallel == 4
    assert config.subagent_timeout_seconds == 300.0


def test_subagent_keys_round_trip_save_load(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import PhosonConfig, load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SUBAGENT_MAX_PARALLEL", raising=False)
    monkeypatch.delenv("PHOSON_SUBAGENT_TIMEOUT", raising=False)

    original = PhosonConfig(
        provider="ollama",
        sessions_dir=home / "sessions",
        subagent_max_parallel=6,
        subagent_timeout_seconds=120.5,
    )
    path = save_config(original)

    # Numeric values are written as TOML numbers, not quoted strings.
    content = path.read_text()
    assert "subagent_max_parallel = 6" in content
    assert "subagent_timeout_seconds = 120.5" in content

    loaded = load_config()
    assert loaded.subagent_max_parallel == 6
    assert loaded.subagent_timeout_seconds == pytest.approx(120.5)
