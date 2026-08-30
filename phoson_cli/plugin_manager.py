"""Community plugin installation and configuration management (I-110)."""

import sys
import subprocess
from dataclasses import dataclass
from collections.abc import Callable
from importlib.metadata import entry_points

from phoson_agent import Plugin, load_plugin

from .config import PhosonConfig, PhosonConfigError, save_config


class PluginManagerError(Exception):
    """A user-actionable plugin installation or management failure."""


@dataclass(frozen=True)
class InstalledPlugin:
    """A configured plugin entry point and its persisted spec."""

    name: str
    spec: str | dict
    enabled: bool


def normalize_plugin_source(source: str) -> str:
    """Normalize the friendly GitHub shorthand to a pip-compatible URL."""
    source = source.strip()
    if source.startswith("github:"):
        target = source.removeprefix("github:")
        repository, separator, revision = target.partition("@")
        if not repository or repository.count("/") != 1:
            raise PluginManagerError(
                "GitHub plugins must use github:owner/repository[@tag-or-commit]"
            )
        suffix = f"@{revision}" if separator and revision else ""
        return f"git+https://github.com/{repository}.git{suffix}"
    if source.startswith("git:https://"):
        return "git+https://" + source.removeprefix("git:https://")
    if not source:
        raise PluginManagerError("Plugin source must not be empty")
    return source


def _entrypoint_names() -> set[str]:
    eps = entry_points()
    if hasattr(eps, "select"):
        return {entry.name for entry in eps.select(group="phoson.plugins")}
    legacy = eps.get("phoson.plugins", []) if hasattr(eps, "get") else []  # type: ignore[union-attr]
    return {entry.name for entry in legacy}


def install_plugin(
    source: str,
    config: PhosonConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Install *source* into the active interpreter and enable its entry point.

    A plugin is executable Python code. Callers must display the source and
    obtain confirmation before invoking this operation unless an explicit
    non-interactive ``--yes`` mode was requested.
    """
    requirement = normalize_plugin_source(source)
    before = _entrypoint_names()
    result = runner(
        ["uv", "pip", "install", "--python", sys.executable, requirement],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PluginManagerError(f"Plugin installation failed: {detail or requirement}")

    added = _entrypoint_names() - before
    if len(added) != 1:
        found = ", ".join(sorted(added)) or "none"
        raise PluginManagerError(
            "Installed package must expose exactly one new entry point in "
            f"'phoson.plugins' (found: {found})."
        )
    name = added.pop()
    spec = f"entrypoint:{name}"
    if spec not in config.plugins:
        config.plugins.append(spec)
        save_config(config, only_fields={"plugins"})
    return name


def configured_plugins(config: PhosonConfig) -> list[InstalledPlugin]:
    """List specs from config without importing or initializing plugin code."""
    result: list[InstalledPlugin] = []
    for spec in config.plugins:
        name = spec if isinstance(spec, str) else str(spec.get("name", "?"))
        result.append(InstalledPlugin(name=name, spec=spec, enabled=True))
    return result


def _matches(spec: str | dict, plugin_id: str) -> bool:
    name = spec if isinstance(spec, str) else str(spec.get("name", ""))
    return name == plugin_id or name == f"entrypoint:{plugin_id}"


def disable_plugin(plugin_id: str, config: PhosonConfig) -> None:
    """Disable a plugin by removing its spec; installed code stays untouched."""
    remaining = [spec for spec in config.plugins if not _matches(spec, plugin_id)]
    if len(remaining) == len(config.plugins):
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    config.plugins = remaining
    save_config(config, only_fields={"plugins"})


def enable_plugin(plugin_id: str, config: PhosonConfig) -> None:
    """Enable a discovered ``phoson.plugins`` entry point by name."""
    if plugin_id not in _entrypoint_names():
        raise PluginManagerError(
            f"No installed phoson.plugins entry point: {plugin_id}"
        )
    spec = f"entrypoint:{plugin_id}"
    if spec not in config.plugins:
        config.plugins.append(spec)
        save_config(config, only_fields={"plugins"})


def remove_plugin(plugin_id: str, config: PhosonConfig) -> None:
    """Remove a plugin from runtime configuration without deleting its package.

    Package uninstallation is deliberately not coupled to this action: several
    entry points may share a distribution, and reliably resolving ownership is
    a separate package-metadata concern. Users may uninstall it with their
    package manager after confirming no other plugin uses it.
    """
    disable_plugin(plugin_id, config)


def doctor_plugin(plugin_id: str, config: PhosonConfig) -> Plugin:
    """Load one configured plugin and verify the public Plugin contract."""
    spec = next((item for item in config.plugins if _matches(item, plugin_id)), None)
    if spec is None:
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    try:
        plugin = load_plugin(spec)
    except Exception as exc:  # noqa: BLE001
        raise PluginManagerError(f"Plugin {plugin_id!r} failed to load: {exc}") from exc
    if not isinstance(plugin, Plugin):  # defensive; loader already enforces this
        raise PhosonConfigError(f"Plugin {plugin_id!r} does not implement Plugin")
    return plugin
