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
    disable_plugin,
    normalize_plugin_source,
)


def test_parse_plugin_subcommand_and_install_alias(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    assert parse_args(["plugin", "list"]).plugin_args == ["list"]
    assert parse_args(["--install-plugin", "github:org/example@v1"]).plugin_args == [
        "install",
        "github:org/example@v1",
    ]


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


def test_enable_plugin_rejects_unknown_entrypoint() -> None:
    with patch("phoson_cli.plugin_manager._entrypoint_names", return_value=set()):
        with pytest.raises(PluginManagerError, match="No installed"):
            enable_plugin("missing", PhosonConfig())
