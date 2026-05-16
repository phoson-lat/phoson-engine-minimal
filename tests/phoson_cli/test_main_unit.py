from phoson_cli.config import PhosonConfig


def test_configured_provider_detects_all_credentials() -> None:
    from phoson_cli.__main__ import _has_configured_provider

    assert _has_configured_provider(
        PhosonConfig(provider="gemini", gemini_api_key="test-key")
    )
    assert _has_configured_provider(
        PhosonConfig(provider="groq", groq_api_key="test-key")
    )
    assert _has_configured_provider(PhosonConfig(provider="ollama"))


def test_main_does_not_run_setup_when_config_file_exists(monkeypatch, tmp_path) -> None:
    import sys

    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    setup_called = False
    repl_ran = False

    async def fake_setup(config):
        nonlocal setup_called
        setup_called = True
        return config

    class FakeRepl:
        def __init__(self, config):
            self.config = config

        async def run(self):
            nonlocal repl_ran
            repl_ran = True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(main_module, "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(main_module, "run_install_wizard", fake_setup)
    monkeypatch.setattr(main_module, "PhosonRepl", FakeRepl)

    main_module.main()

    assert not setup_called
    assert repl_ran
