"""Wiring tests for the bundled SSH plugin (#169)."""

from pathlib import Path

from phoson_cli.config import PhosonConfig
from phoson_plugin_ssh import SshPlugin
from phoson_cli.session_utils import build_ssh_plugins


class TestBuildSshPlugins:
    def test_disabled_returns_empty(self, tmp_path: Path) -> None:
        config = PhosonConfig(provider="ollama", model="m", enable_ssh=False)
        assert build_ssh_plugins(config) == []

    def test_enabled_returns_preconfigured_instance(self, tmp_path: Path) -> None:
        config = PhosonConfig(
            provider="ollama",
            model="m",
            enable_ssh=True,
            ssh_known_hosts=tmp_path / "known_hosts",
            ssh_command_timeout=42,
        )
        specs = build_ssh_plugins(config)
        assert len(specs) == 1
        assert isinstance(specs[0], SshPlugin)
        assert specs[0]._known_hosts == str(tmp_path / "known_hosts")
        assert specs[0]._command_timeout == 42
        # Each call yields a fresh instance (no shared singleton state).
        assert build_ssh_plugins(config)[0] is not specs[0]

    def test_enabled_without_transport_returns_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import phoson_plugin_ssh

        monkeypatch.setattr(phoson_plugin_ssh, "SSH_AVAILABLE", False)
        config = PhosonConfig(provider="ollama", model="m", enable_ssh=True)
        assert build_ssh_plugins(config) == []

    def test_default_disabled(self) -> None:
        config = PhosonConfig(provider="ollama", model="m")
        assert config.enable_ssh is False
        assert config.ssh_known_hosts == Path("~/.ssh/known_hosts").expanduser()
        assert config.ssh_command_timeout == 60.0
