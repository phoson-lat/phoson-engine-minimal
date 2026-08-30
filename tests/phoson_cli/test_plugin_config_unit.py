"""Tests for configured community plugin specs (I-110)."""

from unittest.mock import patch

import pytest

from phoson_agent import Plugin
from phoson_cli.config import PhosonConfig, PhosonConfigError, load_config, save_config
from phoson_cli.session_utils import build_plugin_specs


class _ConfiguredPlugin(Plugin):
    @property
    def name(self) -> str:
        return "configured-plugin"


def test_load_config_reads_plugin_strings_and_inline_tables(
    monkeypatch, tmp_path
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[defaults]\n"
        'plugins = ["entrypoint:plain", '
        '{ name = "entrypoint:configured", '
        'config = { greeting = "Hola", count = 2 } }]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    assert load_config().plugins == [
        "entrypoint:plain",
        {"name": "entrypoint:configured", "config": {"greeting": "Hola", "count": 2}},
    ]


def test_plugin_config_round_trips_without_clobbering_other_content(
    monkeypatch, tmp_path
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        '# preserve this\n[defaults]\ncustom_setting = "keep"\n[other]\nanswer = 42\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    plugins = [
        "entrypoint:plain",
        {"name": "entrypoint:configured", "config": {"greeting": "Hola", "count": 2}},
    ]

    save_config(
        PhosonConfig(provider="ollama", plugins=plugins), only_fields={"plugins"}
    )

    content = config_path.read_text(encoding="utf-8")
    assert "# preserve this" in content
    assert 'custom_setting = "keep"' in content
    assert "[other]" in content
    assert "answer = 42" in content
    assert load_config().plugins == plugins


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('plugins = "entrypoint:nope"\n', "must be an array"),
        ("plugins = [42]\n", "must be a string or inline table"),
        ("plugins = [{}]\n", "requires a string 'name'"),
        ('plugins = [{ name = "entrypoint:x", config = "bad" }]\n', "config must"),
        ('plugins = [{ name = "entrypoint:x", enabled = true }]\n', "only 'name'"),
    ],
)
def test_load_config_rejects_malformed_plugin_specs(
    monkeypatch, tmp_path, value, message
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n" + value, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(PhosonConfigError, match=message):
        load_config()


def test_build_plugin_specs_orders_configured_plugins_before_mcp() -> None:
    configured = _ConfiguredPlugin()
    config = PhosonConfig(plugins=["entrypoint:first"])

    with patch("phoson_cli.session_utils.build_mcp_plugins", return_value=[configured]):
        specs = build_plugin_specs(config)

    assert specs == ["entrypoint:first", configured]
