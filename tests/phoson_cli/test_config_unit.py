import os
from pathlib import Path

import pytest

from phoson_cli.config import PhosonConfig, load_config


def test_history_file_defaults_to_shared_repl_path() -> None:
    """A2: the full-screen and classic front ends share this history file."""
    assert PhosonConfig().history_file == Path("~/.phoson/history.txt").expanduser()


def test_history_file_override_is_not_serialized_or_loaded(
    monkeypatch, tmp_path
) -> None:
    """history_file is a per-run override, not durable configuration."""
    from phoson_cli.config import save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    save_config(
        PhosonConfig(
            provider="ollama",
            history_file=tmp_path / "custom" / "history.txt",
        )
    )

    content = (home / ".phoson" / "config.toml").read_text(encoding="utf-8")
    loaded = load_config()

    assert "history_file" not in content
    assert loaded.history_file == PhosonConfig().history_file


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


def test_reasoning_effort_defaults_to_none(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_REASONING_EFFORT", raising=False)

    assert load_config().reasoning_effort is None


def test_reasoning_effort_env_override(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_REASONING_EFFORT", "high")

    assert load_config().reasoning_effort == "high"


def test_reasoning_effort_round_trip_save_load(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import PhosonConfig, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_REASONING_EFFORT", raising=False)

    save_config(PhosonConfig(provider="ollama", reasoning_effort="medium"))

    assert load_config().reasoning_effort == "medium"


def test_reasoning_effort_narrow_save_only_touches_that_key(
    monkeypatch, tmp_path
) -> None:
    """``/reasoning-effort`` uses ``only_fields={"reasoning_effort"}`` —

    the model set by a previous full save must survive untouched.
    """
    from phoson_cli.config import PhosonConfig, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("PHOSON_MODEL", raising=False)

    save_config(PhosonConfig(provider="ollama", model="llama3"))
    save_config(
        PhosonConfig(provider="ollama", model="llama3", reasoning_effort="low"),
        only_fields={"reasoning_effort"},
    )

    loaded = load_config()
    assert loaded.reasoning_effort == "low"
    assert loaded.model == "llama3"  # untouched by the narrow save


def test_reasoning_effort_off_clears_the_managed_key(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import PhosonConfig, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_REASONING_EFFORT", raising=False)

    save_config(PhosonConfig(provider="ollama", reasoning_effort="high"))
    path = save_config(
        PhosonConfig(provider="ollama", reasoning_effort=None),
        only_fields={"reasoning_effort"},
    )

    assert "reasoning_effort" not in path.read_text()
    assert load_config().reasoning_effort is None


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


# ── save_config preserves user-owned file content ───────────────────────────


class _SaveCfg:
    provider = "groq"
    model = "llama-3.3-70b"
    groq_api_key = "gsk_test"


def test_save_config_preserves_unknown_keys_and_sections(monkeypatch, tmp_path) -> None:
    """User-added keys, comments and extra sections survive a save."""
    from phoson_cli.config import save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    config_path = home / ".phoson" / "config.toml"
    monkeypatch.setenv("HOME", str(home))

    config_path.write_text(
        "# user note at top\n"
        "[defaults]\n"
        'provider = "openrouter"  # was openrouter\n'
        'model = "old-model"\n'
        'fireworks_base_url = "https://custom.proxy/v1"\n'
        'custom_note = "keep me"\n'
        "\n"
        "[custom_section]\n"
        "answer = 42\n",
        encoding="utf-8",
    )

    save_config(_SaveCfg())  # type: ignore[arg-type]
    content = config_path.read_text()

    # user content preserved
    assert "# user note at top" in content
    assert 'fireworks_base_url = "https://custom.proxy/v1"' in content
    assert 'custom_note = "keep me"' in content
    assert "[custom_section]" in content
    assert "answer = 42" in content

    # managed keys updated in place, cleared keys dropped
    assert 'provider = "groq"' in content
    assert 'model = "llama-3.3-70b"' in content
    assert "old-model" not in content

    # still valid TOML, loadable, with the new values
    from phoson_cli.config import load_config

    loaded = load_config()
    assert loaded.provider == "groq"
    assert loaded.model == "llama-3.3-70b"


def test_save_config_preserves_comments_and_order(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    config_path = home / ".phoson" / "config.toml"
    monkeypatch.setenv("HOME", str(home))

    config_path.write_text(
        "[defaults]\n"
        "# my comment inside defaults\n"
        'model = "old"\n'
        'provider = "openrouter"\n',
        encoding="utf-8",
    )

    save_config(_SaveCfg())  # type: ignore[arg-type]
    lines = config_path.read_text().splitlines()

    # the comment and original key order are kept
    assert "# my comment inside defaults" in lines
    model_i = lines.index('model = "llama-3.3-70b"')
    provider_i = lines.index('provider = "groq"')
    comment_i = lines.index("# my comment inside defaults")
    assert comment_i < model_i < provider_i


def test_save_config_removes_cleared_managed_key(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    config_path = home / ".phoson" / "config.toml"
    monkeypatch.setenv("HOME", str(home))

    config_path.write_text(
        "[defaults]\n"
        'provider = "openrouter"\n'
        'subagent_model = "old/sub"\n'
        'model = "old-model"\n',
        encoding="utf-8",
    )

    # _SaveCfg has no subagent_model attribute -> getattr -> None -> dropped
    save_config(_SaveCfg())  # type: ignore[arg-type]
    content = config_path.read_text()
    assert "subagent_model" not in content
    assert "old/sub" not in content
    assert 'provider = "groq"' in content


def test_save_config_creates_defaults_section_when_missing(
    monkeypatch, tmp_path
) -> None:
    from phoson_cli.config import load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    config_path = home / ".phoson" / "config.toml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_MODEL", raising=False)

    config_path.write_text("[other]\nfoo = 1\n", encoding="utf-8")

    save_config(_SaveCfg())  # type: ignore[arg-type]
    content = config_path.read_text()
    assert "[defaults]" in content
    assert "foo = 1" in content

    loaded = load_config()
    assert loaded.provider == "groq"
    assert loaded.model == "llama-3.3-70b"


def test_save_config_idempotent_second_save(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    config_path = home / ".phoson" / "config.toml"
    monkeypatch.setenv("HOME", str(home))

    save_config(_SaveCfg())  # type: ignore[arg-type]
    first = config_path.read_text()
    save_config(_SaveCfg())  # type: ignore[arg-type]
    second = config_path.read_text()

    # no duplicated managed keys on re-save
    assert second == first
    assert second.count('provider = "groq"') == 1
    assert second.count('model = "llama-3.3-70b"') == 1


def test_model_and_reasoning_effort_persist_across_restart(
    monkeypatch, tmp_path
) -> None:
    """Regression for #49: /model + /reasoning-effort must round-trip config."""
    from phoson_cli.config import PhosonConfig, load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_MODEL", raising=False)
    monkeypatch.delenv("PHOSON_REASONING_EFFORT", raising=False)

    # Simulate a mid-session /model + /reasoning-effort change.
    original = PhosonConfig(
        model="anthropic/claude-3.5-haiku",
        reasoning_effort="low",
        sessions_dir=home / "sessions",
    )
    save_config(original)

    # Simulate a fresh CLI process reading the same file.
    loaded = load_config()
    assert loaded.model == "anthropic/claude-3.5-haiku"
    assert loaded.reasoning_effort == "low"


def test_show_reasoning_round_trip(monkeypatch, tmp_path) -> None:
    """Regression for #50: Ctrl+T default persists via show_reasoning field."""
    from phoson_cli.config import PhosonConfig, load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SHOW_REASONING", raising=False)

    original = PhosonConfig(sessions_dir=home / "sessions", show_reasoning=False)
    path = save_config(original, only_fields={"show_reasoning"})
    assert path is not None

    loaded = load_config()
    assert loaded.show_reasoning is False


def test_show_reasoning_defaults_true(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_SHOW_REASONING", raising=False)

    assert load_config().show_reasoning is True
