import pytest

from phoson_cli.config import PhosonConfig, has_configured_provider


def test_configured_provider_detects_all_credentials() -> None:
    assert has_configured_provider(
        PhosonConfig(provider="gemini", gemini_api_key="test-key")
    )
    assert has_configured_provider(
        PhosonConfig(provider="groq", groq_api_key="test-key")
    )
    # Alias: gemini credential enables the "google" alias provider.
    assert has_configured_provider(
        PhosonConfig(provider="google", gemini_api_key="test-key")
    )
    # Local providers need no credential.
    assert has_configured_provider(PhosonConfig(provider="ollama"))
    assert has_configured_provider(PhosonConfig(provider="bedrock"))
    assert has_configured_provider(PhosonConfig(provider="vllm"))
    # Remote provider with no credential at all.
    assert not has_configured_provider(PhosonConfig(provider="openrouter"))
    assert not has_configured_provider(
        PhosonConfig(provider="anthropic", openai_api_key=None)
    )


def test_main_does_not_run_setup_when_config_file_exists(monkeypatch, tmp_path) -> None:
    import sys

    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    setup_called = False
    app_ran = False

    async def fake_setup(config):
        nonlocal setup_called
        setup_called = True
        return config

    class FakeApp:
        def __init__(self, config):
            self.config = config

        async def run_async(self):
            nonlocal app_ran
            app_ran = True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(main_module, "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "run_install_wizard", fake_setup)
    monkeypatch.setattr(main_module, "PhosonApp", FakeApp)

    main_module.main()

    assert not setup_called
    assert app_ran


def test_main_runs_setup_wizard_then_launches_the_full_screen_app(
    monkeypatch, tmp_path
) -> None:
    """First run (no config.toml, no configured provider): the wizard runs

    as a pre-flight step — plain stdout, no full-screen mode — and only
    once it's done does ``main()`` construct ``PhosonApp``. No change was
    needed for this to keep working: the wizard call sits entirely
    before ``PhosonApp(config)`` in ``main()``.
    """
    import sys

    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"  # deliberately does NOT create ~/.phoson

    setup_called = False
    app_ran = False

    async def fake_setup(config):
        nonlocal setup_called
        setup_called = True
        return config

    class FakeApp:
        def __init__(self, config):
            self.config = config

        async def run_async(self):
            nonlocal app_ran
            app_ran = True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(
        main_module, "load_config", lambda: PhosonConfig(provider="openrouter")
    )
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "run_install_wizard", fake_setup)
    monkeypatch.setattr(main_module, "PhosonApp", FakeApp)

    main_module.main()

    assert setup_called
    assert app_ran


def test_main_friendly_error_when_active_provider_lacks_credential(
    monkeypatch, tmp_path, capsys
) -> None:
    """A config file exists but the active provider has no key: main() must
    exit(1) with a friendly message instead of crashing with a traceback."""
    import sys

    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    def fail_build_chat(config):
        raise ValueError("OPENROUTER_API_KEY is required for provider=openrouter")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(
        main_module, "load_config", lambda: PhosonConfig(provider="openrouter")
    )
    monkeypatch.setattr(main_module, "build_chat", fail_build_chat)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is required" in stderr
    assert "phoson-cli --setup" in stderr
