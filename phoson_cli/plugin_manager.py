"""Community plugin installation and configuration management (I-110)."""

import sys
import json
import tomllib
import subprocess
from pathlib import Path
from datetime import UTC, datetime
from dataclasses import dataclass
from collections.abc import Callable
from importlib.metadata import entry_points

from phoson_agent import Plugin, load_plugin

from .config import PhosonConfig, PhosonConfigError, save_config


class PluginManagerError(Exception):
    """A user-actionable plugin installation or management failure."""


_LOCKFILE_NAME = "plugins.lock.toml"


def _lockfile_path() -> Path:
    return Path("~/.phoson").expanduser() / _LOCKFILE_NAME


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _load_lockfile(path: Path | None = None) -> list[dict[str, str]]:
    """Read the private plugin install inventory; malformed data is actionable."""
    file_path = path or _lockfile_path()
    if not file_path.exists():
        return []
    try:
        raw = tomllib.loads(file_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PluginManagerError(
            f"Malformed plugin lockfile {file_path}: {exc}"
        ) from exc
    entries = raw.get("plugin", [])
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) for item in entries
    ):
        raise PluginManagerError(
            f"Malformed plugin lockfile {file_path}: [[plugin]] expected"
        )
    return [{str(key): str(value) for key, value in item.items()} for item in entries]


def _save_lockfile(entries: list[dict[str, str]], path: Path | None = None) -> Path:
    """Persist a minimal, reviewable installation inventory."""
    file_path = path or _lockfile_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# Installed community plugins; managed by phoson-cli.\n"]
    for entry in entries:
        lines.append("[[plugin]]\n")
        for key in ("id", "source", "requirement", "installed_at"):
            value = entry.get(key)
            if value is not None:
                lines.append(f"{key} = {_toml_string(value)}\n")
        lines.append("\n")
    file_path.write_text("".join(lines), encoding="utf-8")
    try:
        file_path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    return file_path


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
    """Return entry points visible to this already-running interpreter."""
    eps = entry_points()
    if hasattr(eps, "select"):
        return {entry.name for entry in eps.select(group="phoson.plugins")}
    legacy = eps.get("phoson.plugins", []) if hasattr(eps, "get") else []  # type: ignore[union-attr]
    return {entry.name for entry in legacy}


def _fresh_entrypoint_names(
    *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> set[str]:
    """Query entry points in a fresh interpreter after an installation.

    ``importlib.metadata`` can cache distribution discovery for the process
    that invoked ``uv pip install``. A child using that exact interpreter sees
    the just-created ``*.dist-info/entry_points.txt`` reliably.
    """
    code = (
        "import json\n"
        "from importlib.metadata import entry_points\n"
        "eps = entry_points()\n"
        "items = eps.select(group='phoson.plugins') if hasattr(eps, 'select') "
        "else eps.get('phoson.plugins', [])\n"
        "print(json.dumps(sorted(ep.name for ep in items)))\n"
    )
    result = runner(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PluginManagerError(
            "Could not inspect plugin entry points after installation: "
            f"{detail or sys.executable}"
        )
    try:
        names = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PluginManagerError("Invalid entry-point inspection output") from exc
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise PluginManagerError("Invalid entry-point inspection output")
    return set(names)


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

    added = _fresh_entrypoint_names(runner=runner) - before
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
    entries = [entry for entry in _load_lockfile() if entry.get("id") != name]
    entries.append(
        {
            "id": name,
            "source": source,
            "requirement": requirement,
            "installed_at": datetime.now(UTC).isoformat(),
        }
    )
    _save_lockfile(entries)
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


def update_plugin(
    plugin_id: str,
    config: PhosonConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Reinstall a plugin from its recorded source and refresh its inventory.

    Update is intentionally explicit and preserves the runtime plugin config.
    A legacy/manual entry without lockfile provenance fails rather than guessing
    a package source or upgrading an unrelated distribution.
    """
    if not any(_matches(spec, plugin_id) for spec in config.plugins):
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    entry = next(
        (item for item in _load_lockfile() if item.get("id") == plugin_id), None
    )
    if entry is None:
        raise PluginManagerError(
            f"No recorded install source for plugin {plugin_id!r}; "
            "reinstall it explicitly"
        )
    requirement = entry.get("requirement")
    if not requirement:
        raise PluginManagerError(
            f"Plugin lockfile entry for {plugin_id!r} lacks a requirement"
        )
    result = runner(
        ["uv", "pip", "install", "--upgrade", "--python", sys.executable, requirement],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PluginManagerError(f"Plugin update failed: {detail or requirement}")
    entries = _load_lockfile()
    for item in entries:
        if item.get("id") == plugin_id:
            item["installed_at"] = datetime.now(UTC).isoformat()
    _save_lockfile(entries)
    return plugin_id


def remove_plugin(plugin_id: str, config: PhosonConfig) -> None:
    """Remove a plugin from runtime configuration without deleting its package.

    Package uninstallation is deliberately not coupled to this action: several
    entry points may share a distribution, and reliably resolving ownership is
    a separate package-metadata concern. Users may uninstall it with their
    package manager after confirming no other plugin uses it.
    """
    disable_plugin(plugin_id, config)
    _save_lockfile(
        [entry for entry in _load_lockfile() if entry.get("id") != plugin_id]
    )


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
