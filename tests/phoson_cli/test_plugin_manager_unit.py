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
    configured_plugins,
    _resolve_git_commit,
    _pin_git_requirement,
    normalize_plugin_source,
)


def test_parse_plugin_subcommand_and_install_alias(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    assert parse_args(["plugin", "list"]).plugin_args == ["list"]
    assert parse_args(["--install-plugin", "github:org/example@v1"]).plugin_args == [
        "install",
        "github:org/example@v1",
    ]


def test_parse_plugin_install_accepts_yes_before_or_after_subcommand(
    monkeypatch,
) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    before = parse_args(["--yes", "plugin", "install", "package==1"])
    after = parse_args(["plugin", "install", "package==1", "--yes"])

    assert before.assume_yes is True and before.plugin_args == ["install", "package==1"]
    assert after.assume_yes is True and after.plugin_args == ["install", "package==1"]


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


def test_git_source_resolves_and_pins_an_immutable_commit() -> None:
    commit = "a" * 40

    def runner(command, **_kwargs):
        assert command == [
            "git",
            "ls-remote",
            "https://github.com/org/example.git",
            "v1",
        ]
        return SimpleNamespace(
            returncode=0, stdout=f"{commit}\trefs/tags/v1\n", stderr=""
        )

    assert _resolve_git_commit("github:org/example@v1", runner=runner) == commit
    assert _pin_git_requirement("github:org/example@v1", commit) == (
        f"git+https://github.com/org/example.git@{commit}"
    )


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


def test_install_reinstalls_local_plugin_with_already_visible_entrypoint(
    tmp_path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project.entry-points."phoson.plugins"]\ndemo = "pkg:create_plugin"\n',
        encoding="utf-8",
    )
    config = PhosonConfig()

    def runner(command, **_kwargs):
        if command[1:2] == ["-c"]:
            return SimpleNamespace(returncode=0, stdout='["demo"]\n', stderr="")
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    with (
        patch("phoson_cli.plugin_manager._entrypoint_names", return_value={"demo"}),
        patch("phoson_cli.plugin_manager._load_lockfile", return_value=[]),
        patch("phoson_cli.plugin_manager._save_lockfile"),
        patch("phoson_cli.plugin_manager.save_config"),
    ):
        assert install_plugin(str(tmp_path), config, runner=runner) == "demo"

    assert config.plugins == ["entrypoint:demo"]


def test_disable_plugin_removes_only_target_and_persists() -> None:
    config = PhosonConfig(plugins=["entrypoint:one", "entrypoint:two"])
    with patch("phoson_cli.plugin_manager.save_config") as save:
        disable_plugin("one", config)

    # F-38: the spec is preserved in disabled_plugins (not just dropped), so
    # it can be re-enabled and shown as disabled in `plugin list`.
    assert config.plugins == ["entrypoint:two"]
    assert config.disabled_plugins == ["entrypoint:one"]
    save.assert_called_once_with(config, only_fields={"plugins", "disabled_plugins"})


def test_disable_then_enable_restores_path_plugin() -> None:
    """F-38: a disabled `path:` plugin (no entry-point name) re-enables."""
    config = PhosonConfig(plugins=["path:/opt/my_plugin.py", "entrypoint:other"])
    with patch("phoson_cli.plugin_manager.save_config"):
        disable_plugin("path:/opt/my_plugin.py", config)
    assert config.plugins == ["entrypoint:other"]
    assert config.disabled_plugins == ["path:/opt/my_plugin.py"]

    # Re-enable: the path spec has no entry point, so it must be restored
    # from disabled_plugins (not rejected as "no installed entry point").
    # ``save_config`` MUST be patched here too: without it this writes the
    # developer's real ``~/.phoson/config.toml`` (plugins + disabled_plugins)
    # and silently un-configures every plugin they had enabled.
    with (
        patch("phoson_cli.plugin_manager._entrypoint_names", return_value=set()),
        patch("phoson_cli.plugin_manager.save_config") as save,
    ):
        enable_plugin("path:/opt/my_plugin.py", config)
    save.assert_called_once_with(config, only_fields={"plugins", "disabled_plugins"})
    assert "path:/opt/my_plugin.py" in config.plugins
    assert config.disabled_plugins == []


def test_configured_plugins_reports_disabled_state() -> None:
    """F-38: `plugin list` reflects disabled plugins, not always 'enabled'."""
    config = PhosonConfig(
        plugins=["entrypoint:one"],
        disabled_plugins=["path:/opt/off.py"],
    )
    entries = configured_plugins(config)
    by_name = {e.name: e for e in entries}
    assert by_name["entrypoint:one"].enabled is True
    assert by_name["path:/opt/off.py"].enabled is False


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
    config = PhosonConfig(
        plugins=["entrypoint:demo", "entrypoint:other"],
        disabled_plugins=["entrypoint:demo", "path:/tmp/demo"],
    )
    with (
        patch("phoson_cli.plugin_manager.save_config") as save,
        patch("phoson_cli.plugin_manager._load_lockfile", return_value=[]),
        patch("phoson_cli.plugin_manager._save_lockfile"),
        patch("phoson_cli.plugin_manager.subprocess.run") as uninstall,
    ):
        remove_plugin("demo", config)
    assert config.plugins == ["entrypoint:other"]
    assert config.disabled_plugins == ["path:/tmp/demo"]
    save.assert_called_once_with(config, only_fields={"plugins", "disabled_plugins"})
    uninstall.assert_not_called()


def test_remove_validates_lockfile_before_mutating_config() -> None:
    config = PhosonConfig(plugins=["entrypoint:demo"])
    with (
        patch(
            "phoson_cli.plugin_manager._load_lockfile",
            side_effect=PluginManagerError("malformed lockfile"),
        ),
        patch("phoson_cli.plugin_manager.save_config") as save,
    ):
        with pytest.raises(PluginManagerError, match="malformed lockfile"):
            remove_plugin("demo", config)

    assert config.plugins == ["entrypoint:demo"]
    assert config.disabled_plugins == []
    save.assert_not_called()


def test_remove_unwritable_lockfile_leaves_config_unchanged() -> None:
    config = PhosonConfig(plugins=["entrypoint:demo"], disabled_plugins=["path:/other"])
    with (
        patch("phoson_cli.plugin_manager._load_lockfile", return_value=[]),
        patch(
            "phoson_cli.plugin_manager._save_lockfile",
            side_effect=OSError("read-only filesystem"),
        ),
        patch("phoson_cli.plugin_manager.save_config") as save,
    ):
        with pytest.raises(OSError, match="read-only"):
            remove_plugin("demo", config)

    assert config.plugins == ["entrypoint:demo"]
    assert config.disabled_plugins == ["path:/other"]
    save.assert_not_called()


def test_remove_rolls_back_inventory_when_config_save_fails() -> None:
    config = PhosonConfig(plugins=["entrypoint:demo"])
    previous_lock = [{"id": "demo", "source": "package"}]
    with (
        patch("phoson_cli.plugin_manager._load_lockfile", return_value=previous_lock),
        patch("phoson_cli.plugin_manager._save_lockfile") as save_lock,
        patch(
            "phoson_cli.plugin_manager.save_config",
            side_effect=[OSError("config read-only"), None],
        ) as save_config,
    ):
        with pytest.raises(OSError, match="config read-only"):
            remove_plugin("demo", config)

    assert config.plugins == ["entrypoint:demo"]
    assert config.disabled_plugins == []
    assert save_lock.call_args_list[0].args == ([],)
    assert save_lock.call_args_list[1].args == (previous_lock,)
    assert save_config.call_count == 2


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


def test_lockfile_failed_atomic_replace_preserves_previous_inventory(
    tmp_path,
) -> None:
    lockfile = tmp_path / "plugins.lock.toml"
    previous = [{"id": "demo", "source": "old"}]
    _save_lockfile(previous, lockfile)

    with patch("pathlib.Path.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            _save_lockfile([{"id": "other", "source": "new"}], lockfile)

    assert _load_lockfile(lockfile) == previous
    assert not list(tmp_path.glob(".*.tmp"))


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
