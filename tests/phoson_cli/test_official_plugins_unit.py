"""The official-plugin table (``phoson_cli.official_plugins``).

A typo in a table entry would otherwise only surface as a "Failed to
initialise ..." warning at runtime, so each entry is checked against the
in-tree package it names.
"""

import importlib
from dataclasses import replace

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.official_plugins import (
    OFFICIAL_PLUGINS,
    OfficialPlugin,
    _in_tree_plugin_path,
    load_official_plugin,
)


@pytest.mark.parametrize("spec", OFFICIAL_PLUGINS, ids=lambda s: s.package)
def test_entry_names_a_real_in_tree_plugin(spec: OfficialPlugin) -> None:
    module = importlib.import_module(spec.package)

    assert callable(getattr(module, spec.class_name))
    assert _in_tree_plugin_path(spec.package).exists()
    if spec.availability is not None:
        flag_module, flag = spec.availability
        assert isinstance(getattr(importlib.import_module(flag_module), flag), bool)
        assert spec.missing_dependency_hint


@pytest.mark.parametrize("spec", OFFICIAL_PLUGINS, ids=lambda s: s.package)
def test_disabled_entry_loads_nothing(spec: OfficialPlugin) -> None:
    disabled = replace(spec, enabled=lambda _config: False)

    assert load_official_plugin(disabled, PhosonConfig()) == []


def test_construction_error_warns_with_the_entry_message(monkeypatch) -> None:
    class _Boom:
        def configure(self, _settings) -> None:
            raise RuntimeError("bad settings")

    monkeypatch.setattr("phoson_plugin_monitor.MonitorPlugin", _Boom)
    spec = OfficialPlugin(
        package="phoson_plugin_monitor",
        class_name="MonitorPlugin",
        enabled=lambda _config: True,
        settings=lambda _config: {},
        disabled_msg="monitors disabled",
        failure_msg="Failed to initialise test plugin: {exc}",
    )

    with pytest.warns(UserWarning, match="Failed to initialise test plugin: bad"):
        assert load_official_plugin(spec, PhosonConfig()) == []
