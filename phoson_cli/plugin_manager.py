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
        for key in (
            "id",
            "source",
            "requirement",
            "resolved_commit",
            "installed_at",
        ):
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


def _git_revision(source: str) -> str | None:
    """Return an explicitly requested Git ref from a supported source."""
    if source.startswith("github:"):
        _repository, separator, revision = source.removeprefix("github:").partition("@")
        return revision if separator and revision else None
    if source.startswith("git+") or source.startswith("git:"):
        _url, separator, revision = source.rpartition("@")
        return revision if separator else None
    return None


def _resolve_git_commit(
    source: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Resolve a Git source's requested ref to an immutable commit SHA."""
    requirement = normalize_plugin_source(source)
    if not requirement.startswith("git+"):
        return None
    url = requirement.removeprefix("git+")
    url_without_revision, separator, _revision = url.rpartition("@")
    if separator:
        url = url_without_revision
    revision = _git_revision(source) or "HEAD"
    result = runner(
        ["git", "ls-remote", url, revision], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PluginManagerError(
            f"Could not resolve Git plugin revision: {detail or source}"
        )
    first_line = next((line for line in result.stdout.splitlines() if line.strip()), "")
    commit = first_line.split("\t", 1)[0].strip()
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit.lower()
    ):
        raise PluginManagerError(f"Could not resolve Git plugin revision: {source}")
    return commit


def _pin_git_requirement(source: str, commit: str | None) -> str:
    """Replace a mutable Git ref in the install requirement with its SHA."""
    requirement = normalize_plugin_source(source)
    if commit is None:
        return requirement
    base, _separator, _revision = requirement.rpartition("@")
    return f"{base or requirement}@{commit}"


def _entrypoint_names() -> set[str]:
    """Return entry points visible to this already-running interpreter."""
    eps = entry_points()
    if hasattr(eps, "select"):
        return {entry.name for entry in eps.select(group="phoson.plugins")}
    legacy = eps.get("phoson.plugins", []) if hasattr(eps, "get") else []  # type: ignore[union-attr]
    return {entry.name for entry in legacy}


def _declared_local_entrypoints(source: str) -> set[str]:
    """Read local-package entry points without importing its plugin code.

    Reinstalling a local development package is idempotent: its entry point
    may already be visible before ``uv pip install``, so an after-minus-before
    delta alone cannot identify it. Its ``pyproject.toml`` is authoritative.
    Remote/PyPI requirements intentionally return an empty set here.
    """
    path = Path(source).expanduser()
    pyproject = path / "pyproject.toml" if path.is_dir() else None
    if pyproject is None or not pyproject.is_file():
        return set()
    try:
        raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PluginManagerError(
            f"Malformed plugin pyproject {pyproject}: {exc}"
        ) from exc
    project = raw.get("project", {})
    entry_points = project.get("entry-points", {}) if isinstance(project, dict) else {}
    group = (
        entry_points.get("phoson.plugins", {}) if isinstance(entry_points, dict) else {}
    )
    if not isinstance(group, dict):
        raise PluginManagerError(
            f"Plugin pyproject {pyproject} has invalid phoson.plugins entry points"
        )
    return {str(name) for name in group}


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
    resolved_commit = _resolve_git_commit(source, runner=runner)
    requirement = _pin_git_requirement(source, resolved_commit)
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

    visible_after = _fresh_entrypoint_names(runner=runner)
    declared_local = _declared_local_entrypoints(source)
    added = visible_after - before
    candidates = declared_local or added
    if len(candidates) != 1 or not candidates <= visible_after:
        found = ", ".join(sorted(candidates)) or "none"
        raise PluginManagerError(
            "Installed package must expose exactly one usable entry point in "
            f"'phoson.plugins' (found: {found})."
        )
    name = candidates.pop()
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
            **({"resolved_commit": resolved_commit} if resolved_commit else {}),
            "installed_at": datetime.now(UTC).isoformat(),
        }
    )
    _save_lockfile(entries)
    return name


def _plugin_name(spec: str | dict) -> str:
    return spec if isinstance(spec, str) else str(spec.get("name", "?"))


def configured_plugins(config: PhosonConfig) -> list[InstalledPlugin]:
    """List specs from config without importing or initializing plugin code.

    F-38: disabled plugins (recorded in ``config.disabled_plugins`` when
    their spec was removed from ``plugins``) are reported too, flagged
    ``enabled=False``, so ``plugin list`` reflects reality instead of
    printing "enabled" for everything and silently dropping disabled ones.
    """
    result: list[InstalledPlugin] = []
    for spec in config.plugins:
        result.append(InstalledPlugin(name=_plugin_name(spec), spec=spec, enabled=True))
    for spec in config.disabled_plugins:
        result.append(
            InstalledPlugin(name=_plugin_name(spec), spec=spec, enabled=False)
        )
    return result


def _matches(spec: str | dict, plugin_id: str) -> bool:
    name = spec if isinstance(spec, str) else str(spec.get("name", ""))
    return name == plugin_id or name == f"entrypoint:{plugin_id}"


def disable_plugin(plugin_id: str, config: PhosonConfig) -> None:
    """Disable a plugin by removing its spec from ``plugins``.

    F-38: the spec is preserved in ``config.disabled_plugins`` rather than
    dropped, so the disabled state is visible and the plugin — including a
    ``path:`` spec, which has no entry-point name to re-derive from — can be
    re-enabled. Installed code is untouched.
    """
    remaining = [spec for spec in config.plugins if not _matches(spec, plugin_id)]
    if len(remaining) == len(config.plugins):
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    removed = [spec for spec in config.plugins if _matches(spec, plugin_id)]
    for spec in removed:
        if not any(
            _matches(existing, plugin_id) for existing in config.disabled_plugins
        ):
            config.disabled_plugins.append(spec)
    config.plugins = remaining
    save_config(config, only_fields={"plugins", "disabled_plugins"})


def enable_plugin(plugin_id: str, config: PhosonConfig) -> None:
    """Enable a plugin by name.

    A plugin that was previously disabled (its spec kept in
    ``config.disabled_plugins``) is restored with its original spec — this
    works for ``path:`` and inline-table specs that have no entry-point name
    to re-derive. Otherwise it must be an installed ``phoson.plugins`` entry
    point, enabled as ``entrypoint:<name>``.
    """
    # Already enabled: idempotent no-op.
    if any(_matches(spec, plugin_id) for spec in config.plugins):
        return
    # Restore a previously-disabled spec (any kind, incl. path:/inline table).
    restored = [spec for spec in config.disabled_plugins if _matches(spec, plugin_id)]
    if restored:
        for spec in restored:
            config.disabled_plugins.remove(spec)
        config.plugins.extend(restored)
        save_config(config, only_fields={"plugins", "disabled_plugins"})
        return
    # Otherwise: a discovered entry point.
    if plugin_id not in _entrypoint_names():
        raise PluginManagerError(
            f"No installed phoson.plugins entry point: {plugin_id}"
        )
    spec = f"entrypoint:{plugin_id}"
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
