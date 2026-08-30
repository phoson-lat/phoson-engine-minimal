"""Tests for community plugin management CLI (I-110)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.__main__ import parse_args
from phoson_cli.plugin_manager import (
    PluginManagerError,
    enable_plugin,
    remove_plugin,
    update_plugin,
    _load_lockfile,
    _save_lockfile,
    disable_plugin,
    install_plugin,
    normalize_plugin_source,
)


def test_parse_plugin_subcommand_and_install_alias(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    assert parse_args(["plugin", "list"]).plugin_args == ["list"]
    assert parse_args(["--install-plugin", "github:org/example@v1"]).plugin_args == [
        "install",
        "github:org/example@v1",
    ]


def test_parse_plugin_command_does_not_consume_non_tty_stdin(monkeypatch) -> None:
    stdin = SimpleNamespace(isatty=lambda: False, read=lambda: "y\n")
    monkeypatch.setattr("sys.stdin", stdin)

    options = parse_args(["plugin", "install", "package==1"])

    assert options.plugin_args == ["install", "package==1"]
    assert options.task is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("github:org/example@v1", "git+https://github.com/org/example.git@v1"),
        ("git:https://example.com/a.git", "git+https://example.com/a.git"),
        ("package==1", "package==1"),
    ],
)
def test_normalize_plugin_source(source, expected) -> None:
    assert normalize_plugin_source(source) == expected


def test_normalize_plugin_source_rejects_bad_github_target() -> None:
    with pytest.raises(PluginManagerError, match="github:owner/repository"):
        normalize_plugin_source("github:not-valid")


def test_install_uses_fresh_interpreter_for_post_install_entrypoints() -> None:
    config = PhosonConfig()
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[1:2] == ["-c"]:
            return SimpleNamespace(returncode=0, stdout='["demo"]\n', stderr="")
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    with (
        patch("phoson_cli.plugin_manager._entrypoint_names", return_value=set()),
        patch("phoson_cli.plugin_manager._load_lockfile", return_value=[]),
        patch("phoson_cli.plugin_manager._save_lockfile"),
        patch("phoson_cli.plugin_manager.save_config"),
    ):
        assert install_plugin("package==1", config, runner=runner) == "demo"

    assert calls[0][:5] == [
        "uv",
        "pip",
        "install",
        "--python",
        __import__("sys").executable,
    ]
    assert calls[1][0] == __import__("sys").executable
    assert calls[1][1] == "-c"
    assert config.plugins == ["entrypoint:demo"]


def test_disable_plugin_removes_only_target_and_persists() -> None:
    config = PhosonConfig(plugins=["entrypoint:one", "entrypoint:two"])
    with patch("phoson_cli.plugin_manager.save_config") as save:
        disable_plugin("one", config)

    assert config.plugins == ["entrypoint:two"]
    save.assert_called_once_with(config, only_fields={"plugins"})


def test_enable_plugin_checks_entrypoint_and_deduplicates() -> None:
    config = PhosonConfig()
    with (
        patch("phoson_cli.plugin_manager._entrypoint_names", return_value={"demo"}),
        patch("phoson_cli.plugin_manager.save_config") as save,
    ):
        enable_plugin("demo", config)
        enable_plugin("demo", config)

    assert config.plugins == ["entrypoint:demo"]
    save.assert_called_once_with(config, only_fields={"plugins"})


def test_remove_plugin_is_a_safe_configuration_only_operation() -> None:
    config = PhosonConfig(plugins=["entrypoint:demo"])
    with patch("phoson_cli.plugin_manager.save_config"):
        remove_plugin("demo", config)
    assert config.plugins == []


def test_lockfile_round_trips_a_reviewable_install_inventory(tmp_path) -> None:
    lockfile = tmp_path / "plugins.lock.toml"
    entries = [
        {
            "id": "demo",
            "source": "github:org/demo@v1",
            "requirement": "git+https://github.com/org/demo.git@v1",
            "installed_at": "2026-08-30T00:00:00+00:00",
        }
    ]

    _save_lockfile(entries, lockfile)

    assert _load_lockfile(lockfile) == entries
    assert lockfile.stat().st_mode & 0o777 == 0o600


def test_update_plugin_uses_locked_requirement_and_preserves_config(tmp_path) -> None:
    config = PhosonConfig(plugins=["entrypoint:demo"])
    lockfile = tmp_path / "plugins.lock.toml"
    _save_lockfile(
        [
            {
                "id": "demo",
                "source": "github:org/demo@v1",
                "requirement": "git+https://github.com/org/demo.git@v1",
                "installed_at": "old",
            }
        ],
        lockfile,
    )
    seen: list[list[str]] = []

    def runner(command, **_kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("phoson_cli.plugin_manager._lockfile_path", return_value=lockfile):
        assert update_plugin("demo", config, runner=runner) == "demo"

    assert seen[0][:5] == ["uv", "pip", "install", "--upgrade", "--python"]
    assert seen[0][-1] == "git+https://github.com/org/demo.git@v1"
    assert config.plugins == ["entrypoint:demo"]
    assert _load_lockfile(lockfile)[0]["installed_at"] != "old"


def test_update_plugin_requires_a_known_locked_source() -> None:
    with pytest.raises(PluginManagerError, match="No recorded install source"):
        update_plugin("demo", PhosonConfig(plugins=["entrypoint:demo"]))


def test_enable_plugin_rejects_unknown_entrypoint() -> None:
    with patch("phoson_cli.plugin_manager._entrypoint_names", return_value=set()):
        with pytest.raises(PluginManagerError, match="No installed"):
            enable_plugin("missing", PhosonConfig())
