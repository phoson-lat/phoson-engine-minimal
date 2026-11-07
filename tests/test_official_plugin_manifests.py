"""Every official in-tree plugin ships an accurate ``phoson-plugin.toml``.

The manifest is read by the loader *before* the plugin is imported, so a
wrong ``id``, a stale tool list or a ``[config]`` schema that rejects the
configuration the CLI really passes would only show up at runtime. These
tests pin each manifest to the plugin code it describes.
"""

import importlib
from typing import Any
from pathlib import Path
from dataclasses import replace

import pytest

from phoson_agent import Plugin
from phoson_cli.config import PhosonConfig
from phoson_agent.plugin_loader import load_plugin
from phoson_cli.official_plugins import OFFICIAL_PLUGINS
from phoson_agent.plugin_manifest import (
    PluginManifest,
    load_manifest,
    find_manifest_for_module,
)

#: package → plugin class exported by it.
PLUGIN_CLASSES: dict[str, str] = {
    "phoson_plugin_bgjobs": "BgJobsPlugin",
    "phoson_plugin_checkpoint": "CheckpointPlugin",
    "phoson_plugin_computeruse": "ComputerUsePlugin",
    "phoson_plugin_mcp": "MCPPlugin",
    "phoson_plugin_memory": "MemoryPlugin",
    "phoson_plugin_monitor": "MonitorPlugin",
    "phoson_plugin_otel": "PhosonOtelPlugin",
    "phoson_plugin_peers": "PeersPlugin",
    "phoson_plugin_questions": "QuestionsPlugin",
    "phoson_plugin_ssh": "SshPlugin",
    "phoson_plugin_stt": "SttPlugin",
    "phoson_plugin_swarm": "SwarmPlugin",
}

PACKAGES = sorted(PLUGIN_CLASSES)

#: Tool lists that cannot be compared without side effects, with the reason.
TOOLS_NOT_COMPARABLE: dict[str, str] = {
    "phoson_plugin_mcp": "MCP tools are discovered from servers at runtime",
}

#: Configs for plugins the CLI does not wire through OFFICIAL_PLUGINS.
EXTRA_CONFIGS: dict[str, dict[str, Any]] = {
    "phoson_plugin_checkpoint": {"dsn": "postgresql://phoson@localhost/phoson"},
    "phoson_plugin_memory": {},
    "phoson_plugin_stt": {},
}


def _cli_config(tmp_path: Path) -> PhosonConfig:
    """A CLI config whose on-disk paths point into *tmp_path*."""
    return replace(
        PhosonConfig(),
        mcp_config_file=tmp_path / "mcps.json",
        monitors_data_dir=tmp_path / "monitors",
        bgjobs_data_dir=tmp_path / "bgjobs",
        peer_name="manifest-test",
        peers_data_dir=tmp_path / "peers",
        ssh_known_hosts=tmp_path / "known_hosts",
        otel_file_path=tmp_path / "trace.json",
    )


def _plugin_config(package: str, tmp_path: Path) -> dict[str, Any]:
    """The ``configure()`` payload the CLI passes for *package*."""
    for spec in OFFICIAL_PLUGINS:
        if spec.package == package:
            return spec.settings(_cli_config(tmp_path))
    return dict(EXTRA_CONFIGS[package])


def _manifest(package: str) -> PluginManifest:
    path = find_manifest_for_module(package)
    assert path is not None, f"{package} ships no phoson-plugin.toml"
    return load_manifest(path)


def _new_plugin(package: str) -> Plugin:
    module = importlib.import_module(package)
    return getattr(module, PLUGIN_CLASSES[package])()


def test_every_official_plugin_is_covered() -> None:
    assert {spec.package for spec in OFFICIAL_PLUGINS} <= set(PLUGIN_CLASSES)
    assert set(EXTRA_CONFIGS) == set(PLUGIN_CLASSES) - {
        spec.package for spec in OFFICIAL_PLUGINS
    }


@pytest.mark.parametrize("package", PACKAGES)
def test_manifest_identity_matches_plugin(package: str) -> None:
    manifest = _manifest(package)
    plugin = _new_plugin(package)

    assert manifest.id == plugin.name
    assert manifest.entry == f"{package}:plugin"
    assert manifest.incompatibility(None) is None


@pytest.mark.parametrize("package", PACKAGES)
def test_manifest_provides_matches_plugin(package: str, tmp_path: Path) -> None:
    manifest = _manifest(package)
    plugin = _new_plugin(package)
    plugin.configure(_plugin_config(package, tmp_path))
    if package == "phoson_plugin_memory":
        # get_tools() only closes over the backend that initialize() would
        # connect; a stand-in keeps the check free of Redis/Postgres.
        plugin.backend = object()  # type: ignore[attr-defined]

    commands = [name for spec in plugin.get_commands() for name in spec.names]
    assert list(manifest.provides.commands) == commands
    assert manifest.provides.middlewares == bool(plugin.get_middlewares())
    assert manifest.provides.theme == (plugin.get_theme_extension() is not None)

    if package in TOOLS_NOT_COMPARABLE:
        assert manifest.provides.tools == ()
        pytest.skip(f"tool names not compared: {TOOLS_NOT_COMPARABLE[package]}")
    assert list(manifest.provides.tools) == [tool.name for tool in plugin.get_tools()]


@pytest.mark.parametrize("package", PACKAGES)
def test_plugin_loads_through_manifest_with_cli_config(
    package: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The package loader returns the module-level singleton; swap in a fresh
    # instance so configuring it here cannot leak into other tests.
    module = importlib.import_module(package)
    monkeypatch.setattr(module, "plugin", _new_plugin(package))

    plugin = load_plugin(
        {"name": package, "config": _plugin_config(package, tmp_path)},
        initialize=False,
    )

    assert plugin.manifest is not None
    assert plugin.manifest.id == plugin.name
