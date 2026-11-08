"""`phoson-cli plugin config`: show, set and unset plugin settings."""

import pytest

from phoson_agent import parse_manifest
from phoson_cli.config import PhosonConfig
from phoson_cli.__main__ import _run_plugin_command
from phoson_cli.plugin_manager import (
    PluginManagerError,
    set_plugin_config,
    plugin_config_view,
    unset_plugin_config,
)

MANIFEST = parse_manifest(
    {
        "schema": 1,
        "id": "notes",
        "version": "1.0.0",
        "entry": "pi_notes:plugin",
        "compat": {"phoson": ">=0"},
        "config": {
            "dir": {"type": "string", "default": "~/notes", "description": "Where"},
            "limit": {"type": "integer", "choices": [10, 50]},
            "token": {"type": "string", "secret": True, "env": "NOTES_TOKEN"},
            "tags": {"type": "array"},
            "enabled": {"type": "boolean", "required": True},
        },
    }
)


@pytest.fixture
def setup(monkeypatch):
    saved: list[set[str]] = []
    monkeypatch.setattr(
        "phoson_cli.plugin_manager.save_config",
        lambda _config, only_fields=None: saved.append(set(only_fields or ())),
    )
    monkeypatch.setattr(
        "phoson_cli.plugin_manager._manifest_info", lambda _spec: (MANIFEST, None)
    )
    monkeypatch.setattr("phoson_cli.plugin_manager.activate_plugin_paths", lambda: [])
    config = PhosonConfig(plugins=["entrypoint:notes"])
    return config, saved


def test_view_reports_value_sources_and_problems(setup) -> None:
    config, _ = setup
    view = plugin_config_view("notes", config, environ={"NOTES_TOKEN": "s3cret"})
    sources = {row.spec.key: row.source for row in view.rows}
    assert sources == {
        "dir": "default",
        "limit": "unset",
        "token": "env $NOTES_TOKEN",
        "tags": "unset",
        "enabled": "unset",
    }
    text = "\n".join(view.lines())
    assert "s3cret" not in text and "********" in text
    assert any("enabled" in p and "required" in p for p in view.problems)


def test_set_coerces_validates_and_stores_inline_table(setup) -> None:
    config, saved = setup
    assert set_plugin_config("notes", "limit", "50", config)[0] == 50
    assert set_plugin_config("notes", "enabled", "yes", config)[0] is True
    assert set_plugin_config("notes", "tags", '["a", "b"]', config)[0] == ["a", "b"]
    assert config.plugins == [
        {
            "name": "entrypoint:notes",
            "config": {"limit": 50, "enabled": True, "tags": ["a", "b"]},
        }
    ]
    assert saved[-1] == {"plugins", "disabled_plugins"}
    assert plugin_config_view("notes", config, environ={}).problems == []


@pytest.mark.parametrize(
    ("key", "raw", "message"),
    [
        ("limit", "abc", "not an integer"),
        ("limit", "20", "not one of"),
        ("tags", "[oops", "not valid JSON"),
        ("tags", '{"a": 1}', "expected array"),
        ("nope", "1", "Unknown config key"),
    ],
)
def test_set_rejects_invalid_values(setup, key, raw, message) -> None:
    config, saved = setup
    with pytest.raises(PluginManagerError, match=message):
        set_plugin_config("notes", key, raw, config)
    assert config.plugins == ["entrypoint:notes"]
    assert saved == []


def test_unset_reverts_to_plain_spec(setup) -> None:
    config, _ = setup
    set_plugin_config("notes", "limit", "10", config)
    assert unset_plugin_config("notes", "limit", config) is True
    assert config.plugins == ["entrypoint:notes"]
    assert unset_plugin_config("notes", "limit", config) is False


def test_disabled_plugins_are_configurable(setup) -> None:
    config, _ = setup
    config.plugins, config.disabled_plugins = [], ["entrypoint:notes"]
    set_plugin_config("notes", "limit", "10", config)
    assert config.disabled_plugins[0]["config"] == {"limit": 10}


def test_plugin_without_schema_cannot_be_set(setup, monkeypatch) -> None:
    config, _ = setup
    monkeypatch.setattr(
        "phoson_cli.plugin_manager._manifest_info", lambda _spec: (None, None)
    )
    with pytest.raises(PluginManagerError, match="declares no \\[config\\] schema"):
        set_plugin_config("notes", "limit", "10", config)
    assert "no [config] schema" in plugin_config_view("notes", config).lines()[0]


def test_unknown_plugin(setup) -> None:
    config, _ = setup
    with pytest.raises(PluginManagerError, match="not found"):
        plugin_config_view("ghost", config)


def test_cli_commands(setup, capsys, monkeypatch) -> None:
    config, _ = setup
    monkeypatch.delenv("NOTES_TOKEN", raising=False)
    assert _run_plugin_command(["config", "notes", "set", "token", "abc"], config) == 0
    out = capsys.readouterr().out
    assert "Set notes.token = ********" in out
    assert "plain text" in out and "$NOTES_TOKEN" in out

    assert _run_plugin_command(["config", "notes", "set", "enabled", "1"], config) == 0
    assert _run_plugin_command(["config", "notes"], config) == 0
    out = capsys.readouterr().out
    assert "token (string, secret) = ********  [config]" in out
    assert "abc" not in out

    assert _run_plugin_command(["config", "notes", "set", "limit", "7"], config) == 1
    assert "not one of" in capsys.readouterr().err

    assert _run_plugin_command(["config", "notes", "unset", "token"], config) == 0
    assert "Unset notes.token" in capsys.readouterr().out
