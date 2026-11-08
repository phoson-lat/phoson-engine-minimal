"""Community plugin installation and configuration management (I-110).

Install model (see ``docs/research/plugin-manifest-spec.md``):

* Every community plugin is installed into its **own directory**,
  ``~/.phoson/plugins/<id>/`` (``uv pip install --target``), never into the
  CLI's environment. The host's installed versions are passed as
  constraints, so a plugin cannot downgrade or replace the CLI's own
  dependencies; copies of host packages are pruned from the plugin directory
  after the install. At startup :func:`activate_plugin_paths` appends each
  plugin directory to ``sys.path`` (after the host's own paths, so host
  packages always win) and the ``phoson.plugins`` entry points become
  visible.
* Installing is *stage → review → commit*: the package is installed into a
  staging directory, its ``phoson-plugin.toml`` (if any) is read without
  importing the plugin, the review (id, version, capabilities, provides) is
  shown for confirmation, and only then is the directory moved into place.
* Uninstalling deletes the plugin directory, its config entries and its
  lockfile record. Legacy installs made into the CLI environment by older
  versions (lockfile entries without ``location``) are only removed from the
  configuration, as before.
"""

import os
import sys
import shutil
import tomllib
import tempfile
import importlib
import subprocess
import importlib.metadata
from typing import Any
from pathlib import Path
from datetime import UTC, datetime
from dataclasses import field, dataclass
from collections.abc import Callable
from importlib.metadata import Distribution, entry_points

from packaging.utils import canonicalize_name

from phoson_agent import Plugin, PluginManifest, load_plugin, load_manifest
from phoson_agent.plugin_loader import find_plugin_manifest
from phoson_agent.plugin_manifest import (
    MANIFEST_FILENAME,
    ConfigField,
    PluginManifestError,
    engine_version,
)

from .config import PhosonConfig, PhosonConfigError, save_config


class PluginManagerError(Exception):
    """A user-actionable plugin installation or management failure."""


_LOCKFILE_NAME = "plugins.lock.toml"


_ENTRY_POINT_GROUP = "phoson.plugins"

#: Lockfile keys, in the order they are written.
_LOCK_KEYS = (
    "id",
    "source",
    "requirement",
    "resolved_commit",
    "version",
    "distribution",
    "location",
    "installed_at",
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _phoson_home() -> Path:
    return Path("~/.phoson").expanduser()


def _lockfile_path() -> Path:
    return _phoson_home() / _LOCKFILE_NAME


def plugins_dir() -> Path:
    """Root of the isolated plugin installs (``PHOSON_PLUGINS_DIR`` overrides)."""
    override = os.environ.get("PHOSON_PLUGINS_DIR")
    return Path(override).expanduser() if override else _phoson_home() / "plugins"


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
        for key in _LOCK_KEYS:
            value = entry.get(key)
            if value is not None:
                lines.append(f"{key} = {_toml_string(value)}\n")
        lines.append("\n")
    temporary = file_path.with_name(f".{file_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text("".join(lines), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass
        temporary.replace(file_path)
    finally:
        temporary.unlink(missing_ok=True)
    return file_path


@dataclass(frozen=True)
class InstalledPlugin:
    """A configured plugin entry point and its persisted spec.

    ``manifest`` is the plugin's ``phoson-plugin.toml`` when it ships one
    (read without importing the plugin); ``manifest_error`` explains an
    invalid one.
    """

    name: str
    spec: str | dict
    enabled: bool
    manifest: PluginManifest | None = None
    manifest_error: str | None = None


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


# ── Isolated plugin directories ──────────────────────────────────────────────


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _locked_location(entry: dict[str, str]) -> Path | None:
    """The plugin directory recorded in a lockfile entry, if it is valid.

    Only directories *inside* :func:`plugins_dir` are honoured, so a damaged
    or hand-edited lockfile can never make the CLI import from, or delete,
    an arbitrary path.
    """
    location = entry.get("location")
    if not location:
        return None
    path = Path(location)
    root = plugins_dir()
    if not _inside(path, root) or path.resolve() == root.resolve():
        return None
    return path


def activate_plugin_paths() -> list[Path]:
    """Make isolated plugin installs importable in this process.

    Appends each installed plugin directory to ``sys.path`` (after the host's
    paths, so the host's own packages always take precedence) and returns the
    directories added. Safe to call repeatedly; a malformed lockfile is
    ignored here (``plugin list`` reports it).
    """
    try:
        entries = _load_lockfile()
    except PluginManagerError:
        return []
    added: list[Path] = []
    for entry in entries:
        location = _locked_location(entry)
        if location is None or not location.is_dir():
            continue
        text = str(location)
        if text not in sys.path:
            sys.path.append(text)
            added.append(location)
    if added:
        importlib.invalidate_caches()
    return added


def _entrypoint_names() -> set[str]:
    """Return entry points visible to this already-running interpreter."""
    activate_plugin_paths()
    eps = entry_points()
    if hasattr(eps, "select"):
        return {entry.name for entry in eps.select(group=_ENTRY_POINT_GROUP)}
    legacy = eps.get(_ENTRY_POINT_GROUP, []) if hasattr(eps, "get") else []  # type: ignore[union-attr]
    return {entry.name for entry in legacy}


# ── Installation ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginReview:
    """What is about to be installed, shown to the user before committing."""

    id: str
    version: str
    distribution: str
    source: str
    requirement: str
    manifest: PluginManifest | None
    replaces: str | None = None
    warnings: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        if self.manifest is not None:
            lines = self.manifest.summary_lines()
        else:
            lines = [
                f"{self.id} {self.version}",
                "  (no phoson-plugin.toml: capabilities and config are undeclared)",
            ]
        lines.append(f"  package: {self.distribution} from {self.source}")
        if self.replaces:
            lines.append(f"  replaces installed version {self.replaces}")
        if self.manifest is not None:
            from phoson_agent.permissions import capability_hints

            if capability_hints(self.manifest.capabilities) is not None:
                lines.append(
                    "  permissions: its tools ask before running (it declares "
                    "shell or filesystem writes); auto mode or a per-tool rule "
                    "overrides this"
                )
        lines.extend(f"  warning: {warning}" for warning in self.warnings)
        return lines


ConfirmFn = Callable[[PluginReview], bool]


def _is_frozen() -> bool:
    from ._frozen import is_frozen

    return is_frozen()


def _installer_command(
    target: Path,
    requirement: str,
    constraints: Path | None,
    *more_requirements: str,
) -> list[str]:
    """The command that installs *requirement* into *target*.

    ``uv`` is preferred; ``pip`` (``python -m pip``) is the fallback for
    regular installs. The standalone binary has no interpreter of its own to
    hand to the installer, so it requires ``uv`` and targets the bundled
    Python version; when none is on ``PATH`` a pinned, checksum-verified
    copy is downloaded once into ``~/.phoson/bin`` (:mod:`.managed_uv`).
    """
    from .managed_uv import ManagedUvError, find_uv, ensure_uv

    uv = find_uv()
    if uv is None and _is_frozen():
        try:
            uv = ensure_uv(notify=lambda msg: print(msg, file=sys.stderr))
        except ManagedUvError as exc:
            raise PluginManagerError(str(exc)) from exc
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if uv is not None:
        command = [uv, "pip", "install", "--target", str(target)]
        if _is_frozen():
            command += ["--python-version", version]
        else:
            command += ["--python", sys.executable]
        if constraints is not None:
            command += ["--constraint", str(constraints)]
        return [*command, requirement, *more_requirements]
    command = [sys.executable, "-m", "pip", "install", "--target", str(target)]
    if constraints is not None:
        command += ["--constraint", str(constraints)]
    return [*command, requirement, *more_requirements]


def _uv_executable() -> str:
    """``uv`` for direct invocations: on PATH, else the private copy."""
    from .managed_uv import find_uv

    return find_uv() or "uv"


def _host_distributions(
    exclude: tuple[Path, ...] = (),
) -> dict[str, Distribution]:
    """Distributions importable by the host (first on ``sys.path`` wins).

    Copies under the plugins directory (or any of *exclude*) are private
    installs, not the host's, and are skipped.
    """
    roots = (plugins_dir(), *exclude)
    found: dict[str, Distribution] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name") if dist.metadata else None
        if not name:
            continue
        located = Path(str(dist.locate_file("")))
        if any(_inside(located, root) for root in roots):
            continue  # another plugin's private copy, not the host's
        found.setdefault(canonicalize_name(name), dist)
    return found


def _host_constraints(host: dict[str, Distribution]) -> list[str]:
    """``name==version`` pins for host packages a plugin must not replace.

    Local/editable/VCS installs are skipped: their version cannot be fetched
    from an index, and pinning them would make every resolution fail.
    """
    pins: list[str] = []
    for name, dist in sorted(host.items()):
        direct = dist.read_text("direct_url.json") or ""
        if '"dir_info"' in direct or '"vcs_info"' in direct or '"file://' in direct:
            continue
        pins.append(f"{name}=={dist.version}")
    return pins


def _staged_distributions(staging: Path) -> list[Distribution]:
    return list(importlib.metadata.distributions(path=[str(staging)]))


def _staged_plugin(
    staging: Path, host: dict[str, Distribution]
) -> tuple[str, str, Distribution]:
    """Find the one ``phoson.plugins`` entry point the install provides.

    Returns ``(entry point name, entry point module, distribution)``.
    """
    candidates: list[tuple[str, str, Distribution]] = []
    for dist in _staged_distributions(staging):
        name = canonicalize_name(dist.metadata.get("Name") or "")
        if name in host:
            continue
        for ep in dist.entry_points:
            if ep.group == _ENTRY_POINT_GROUP:
                candidates.append((ep.name, ep.module, dist))
    if len(candidates) != 1:
        found = ", ".join(sorted(name for name, _m, _d in candidates)) or "none"
        raise PluginManagerError(
            "Installed package must expose exactly one usable entry point in "
            f"'{_ENTRY_POINT_GROUP}' (found: {found})."
        )
    return candidates[0]


def _staged_manifest(staging: Path, module: str) -> PluginManifest | None:
    path = staging / module.split(".", 1)[0] / MANIFEST_FILENAME
    return load_manifest(path) if path.is_file() else None


def _prune_host_copies(
    staging: Path, host: dict[str, Distribution], keep: str
) -> list[str]:
    """Delete staged copies of packages the host already provides.

    Host packages always win on ``sys.path`` (plugin directories are
    appended), so these copies would be dead weight; with the host pinned as
    a constraint they are the same versions anyway. Returns pruned names.
    """
    pruned: list[str] = []
    for dist in _staged_distributions(staging):
        name = canonicalize_name(dist.metadata.get("Name") or "")
        if not name or name == keep or name not in host:
            continue
        for file in dist.files or []:
            path = Path(str(dist.locate_file(file)))
            if _inside(path, staging) and path.is_file():
                path.unlink(missing_ok=True)
        info_dir = getattr(dist, "_path", None)
        if info_dir is not None and _inside(Path(info_dir), staging):
            shutil.rmtree(info_dir, ignore_errors=True)
        pruned.append(name)
    # Drop directories emptied by the pruning (bottom-up).
    for directory in sorted(
        (p for p in staging.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return pruned


def _commit_directory(staging: Path, final: Path) -> None:
    """Move *staging* to *final*, replacing an existing install atomically
    enough: the old directory is kept aside until the new one is in place."""
    backup: Path | None = None
    if final.exists():
        backup = final.with_name(f".{final.name}.old-{os.getpid()}")
        shutil.rmtree(backup, ignore_errors=True)
        final.rename(backup)
    try:
        staging.rename(final)
    except OSError:
        if backup is not None:
            backup.rename(final)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _add_plugin_spec(config: PhosonConfig, plugin_id: str) -> None:
    """Enable ``entrypoint:<id>`` unless some spec (str or dict) already does."""
    if any(_matches(spec, plugin_id) for spec in config.plugins):
        return
    config.disabled_plugins = [
        spec for spec in config.disabled_plugins if not _matches(spec, plugin_id)
    ]
    config.plugins.append(f"entrypoint:{plugin_id}")
    save_config(config, only_fields={"plugins", "disabled_plugins"})


def install_plugin(
    source: str,
    config: PhosonConfig,
    *,
    runner: Runner = subprocess.run,
    confirm: ConfirmFn | None = None,
) -> str | None:
    """Install *source* into its own plugin directory and enable it.

    Stage → review → commit: the package is installed into a staging
    directory under :func:`plugins_dir`, its manifest is read (without
    importing the plugin) and checked for compatibility, and *confirm* — when
    given — receives a :class:`PluginReview`; returning False aborts with
    nothing installed. Building a source distribution or a Git checkout runs
    that project's build backend, so callers must still confirm the *source*
    before calling this unless a non-interactive ``--yes`` was requested.

    Returns the plugin id, or ``None`` when *confirm* declined.
    """
    resolved_commit = _resolve_git_commit(source, runner=runner)
    requirement = _pin_git_requirement(source, resolved_commit)
    root = plugins_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        host = _host_distributions()
        constraints_file = staging.parent / f"{staging.name}.constraints.txt"
        pins = _host_constraints(host)
        constraints_file.write_text("\n".join(pins) + "\n", encoding="utf-8")
        try:
            result = runner(
                _installer_command(
                    staging, requirement, constraints_file if pins else None
                ),
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            constraints_file.unlink(missing_ok=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PluginManagerError(
                f"Plugin installation failed: {detail or requirement}"
            )

        ep_name, ep_module, dist = _staged_plugin(staging, host)
        dist_name = canonicalize_name(dist.metadata.get("Name") or ep_name)
        dist_version = dist.version
        manifest = _staged_manifest(staging, ep_module)
        warnings: list[str] = []
        if manifest is not None:
            if manifest.id != ep_name:
                raise PluginManagerError(
                    f"Manifest id {manifest.id!r} does not match the "
                    f"'{_ENTRY_POINT_GROUP}' entry point name {ep_name!r}."
                )
            reason = manifest.incompatibility(engine_version())
            if reason:
                raise PluginManagerError(
                    f"Plugin {manifest.id!r} {manifest.version} is not compatible: "
                    f"{reason}."
                )
            if manifest.version != dist_version:
                warnings.append(
                    f"manifest version {manifest.version} differs from the "
                    f"package version {dist_version}"
                )
        if dist_name in host:
            warnings.append(
                f"{dist_name} is also installed in the CLI environment; that copy "
                "takes precedence (uninstall it there to use this one)"
            )
        _prune_host_copies(staging, host, keep=dist_name)
        # Installer bookkeeping (uv's --target lock) is not part of the plugin.
        (staging / ".lock").unlink(missing_ok=True)

        previous = next((e for e in _load_lockfile() if e.get("id") == ep_name), None)
        review = PluginReview(
            id=ep_name,
            version=dist_version,
            distribution=dist_name,
            source=source,
            requirement=requirement,
            manifest=manifest,
            replaces=previous.get("version") if previous else None,
            warnings=tuple(warnings),
        )
        if confirm is not None and not confirm(review):
            return None

        final = root / ep_name
        _commit_directory(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _add_plugin_spec(config, ep_name)
    entries = [entry for entry in _load_lockfile() if entry.get("id") != ep_name]
    entries.append(
        {
            "id": ep_name,
            "source": source,
            "requirement": requirement,
            **({"resolved_commit": resolved_commit} if resolved_commit else {}),
            "version": dist_version,
            "distribution": dist_name,
            "location": str(final),
            "installed_at": datetime.now(UTC).isoformat(),
        }
    )
    _save_lockfile(entries)
    activate_plugin_paths()
    return ep_name


def _plugin_name(spec: str | dict) -> str:
    return spec if isinstance(spec, str) else str(spec.get("name", "?"))


def _manifest_info(spec: str | dict) -> tuple[PluginManifest | None, str | None]:
    """A configured plugin's manifest, read without importing it."""
    try:
        return find_plugin_manifest(spec), None
    except Exception as exc:  # noqa: BLE001 - reported, never fatal for listing
        return None, str(exc)


def configured_plugins(config: PhosonConfig) -> list[InstalledPlugin]:
    """List specs from config without importing or initializing plugin code.

    F-38: disabled plugins (recorded in ``config.disabled_plugins`` when
    their spec was removed from ``plugins``) are reported too, flagged
    ``enabled=False``, so ``plugin list`` reflects reality instead of
    printing "enabled" for everything and silently dropping disabled ones.
    Each entry carries the plugin's manifest when it ships one.
    """
    activate_plugin_paths()
    result: list[InstalledPlugin] = []
    for specs, enabled in ((config.plugins, True), (config.disabled_plugins, False)):
        for spec in specs:
            manifest, error = _manifest_info(spec)
            result.append(
                InstalledPlugin(
                    name=_plugin_name(spec),
                    spec=spec,
                    enabled=enabled,
                    manifest=manifest,
                    manifest_error=error,
                )
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
    runner: Runner = subprocess.run,
    confirm: ConfirmFn | None = None,
) -> str | None:
    """Reinstall a plugin from its recorded *source* and refresh its inventory.

    For isolated installs this re-runs the stage → review → commit install
    from the recorded source (a Git tag/branch is re-resolved, so an update
    can actually move to a newer commit) and keeps the runtime config.
    Legacy installs into the CLI environment are upgraded in place, as
    before. An entry without lockfile provenance fails rather than guessing a
    package source.
    """
    configured = any(
        _matches(spec, plugin_id)
        for spec in [*config.plugins, *config.disabled_plugins]
    )
    if not configured:
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    entry = next(
        (item for item in _load_lockfile() if item.get("id") == plugin_id), None
    )
    if entry is None:
        raise PluginManagerError(
            f"No recorded install source for plugin {plugin_id!r}; "
            "reinstall it explicitly"
        )
    if _locked_location(entry) is not None:
        source = entry.get("source") or entry.get("requirement")
        if not source:
            raise PluginManagerError(
                f"Plugin lockfile entry for {plugin_id!r} lacks a source"
            )
        installed = install_plugin(source, config, runner=runner, confirm=confirm)
        if installed is not None and installed != plugin_id:
            raise PluginManagerError(
                f"Source {source!r} now provides plugin {installed!r}, "
                f"not {plugin_id!r}"
            )
        return installed

    requirement = entry.get("requirement")
    if not requirement:
        raise PluginManagerError(
            f"Plugin lockfile entry for {plugin_id!r} lacks a requirement"
        )
    result = runner(
        [
            _uv_executable(),
            "pip",
            "install",
            "--upgrade",
            "--python",
            sys.executable,
            requirement,
        ],
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
    """Remove a plugin from runtime configuration without deleting its files.

    Specs are deleted from both enabled and disabled configuration and the
    lockfile record is dropped. :func:`uninstall_plugin` additionally deletes
    an isolated install's directory.
    """
    previous_plugins = list(config.plugins)
    previous_disabled = list(config.disabled_plugins)
    previous_lock = _load_lockfile()
    plugins = [spec for spec in previous_plugins if not _matches(spec, plugin_id)]
    disabled = [spec for spec in previous_disabled if not _matches(spec, plugin_id)]
    if plugins == previous_plugins and disabled == previous_disabled:
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    next_lock = [entry for entry in previous_lock if entry.get("id") != plugin_id]

    # Persist the inventory first. If it is unavailable, runtime configuration
    # remains untouched. A later config failure rolls both stores back.
    _save_lockfile(next_lock)
    config.plugins = plugins
    config.disabled_plugins = disabled
    try:
        save_config(config, only_fields={"plugins", "disabled_plugins"})
    except Exception:
        config.plugins = previous_plugins
        config.disabled_plugins = previous_disabled
        try:
            _save_lockfile(previous_lock)
            save_config(config, only_fields={"plugins", "disabled_plugins"})
        except Exception as rollback_error:  # noqa: BLE001
            raise PluginManagerError(
                f"Plugin removal failed and rollback was incomplete: {rollback_error}"
            ) from rollback_error
        raise


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of :func:`uninstall_plugin`."""

    plugin_id: str
    deleted: Path | None
    note: str | None = None


def uninstall_plugin(plugin_id: str, config: PhosonConfig) -> UninstallResult:
    """Remove a plugin's configuration, lockfile record and installed files.

    Files are deleted only for isolated installs whose directory is inside
    :func:`plugins_dir`. A legacy install made into the CLI environment is
    removed from the configuration only, with a note explaining how to drop
    the package (several entry points may share one distribution there).
    """
    entry = next(
        (item for item in _load_lockfile() if item.get("id") == plugin_id), None
    )
    location = _locked_location(entry) if entry is not None else None
    configured = any(
        _matches(spec, plugin_id)
        for spec in [*config.plugins, *config.disabled_plugins]
    )
    if configured:
        remove_plugin(plugin_id, config)
    elif entry is not None:
        _save_lockfile([e for e in _load_lockfile() if e.get("id") != plugin_id])
    else:
        raise PluginManagerError(f"Plugin not found: {plugin_id}")

    if location is not None and location.exists():
        shutil.rmtree(location)
        text = str(location)
        if text in sys.path:
            sys.path.remove(text)
        return UninstallResult(plugin_id, deleted=location)
    note = None
    if entry is not None and entry.get("distribution") is None:
        note = (
            "this plugin was installed into the CLI environment by an older "
            "phoson; remove the package with `uv pip uninstall <package>` if "
            "nothing else uses it"
        )
    return UninstallResult(plugin_id, deleted=None, note=note)


@dataclass(frozen=True)
class DoctorReport:
    """Result of :func:`doctor_plugin`: the loaded plugin and any findings."""

    plugin: Plugin
    problems: tuple[str, ...] = field(default_factory=tuple)


def doctor_plugin(plugin_id: str, config: PhosonConfig) -> DoctorReport:
    """Load one configured plugin *without initializing it* and check it.

    Verifies the public Plugin contract and, when the plugin ships a
    manifest, that what it actually provides matches what it declares.
    ``initialize()`` is not called, so checking a plugin has no side effects
    (claiming a peer name, opening connections, starting watchers).
    """
    spec = next(
        (
            item
            for item in [*config.plugins, *config.disabled_plugins]
            if _matches(item, plugin_id)
        ),
        None,
    )
    if spec is None:
        raise PluginManagerError(f"Configured plugin not found: {plugin_id}")
    activate_plugin_paths()
    try:
        plugin = load_plugin(spec, initialize=False)
    except Exception as exc:  # noqa: BLE001
        raise PluginManagerError(f"Plugin {plugin_id!r} failed to load: {exc}") from exc
    if not isinstance(plugin, Plugin):  # defensive; loader already enforces this
        raise PhosonConfigError(f"Plugin {plugin_id!r} does not implement Plugin")
    return DoctorReport(plugin=plugin, problems=tuple(provides_mismatches(plugin)))


def provides_mismatches(plugin: Plugin) -> list[str]:
    """Differences between a loaded plugin and its manifest's ``[provides]``."""
    manifest = plugin.manifest
    if manifest is None:
        return []
    problems: list[str] = []
    if plugin.name != manifest.id:
        problems.append(f"Plugin.name {plugin.name!r} != manifest id {manifest.id!r}")
    try:
        tools = {tool.name for tool in plugin.get_tools()}
    except Exception as exc:  # noqa: BLE001
        return [*problems, f"get_tools() failed: {exc}"]
    declared = set(manifest.provides.tools)
    if declared and tools != declared:
        missing = sorted(declared - tools)
        extra = sorted(tools - declared)
        if missing:
            problems.append(f"declared tools not provided: {', '.join(missing)}")
        if extra:
            problems.append(f"tools not declared in the manifest: {', '.join(extra)}")
    elif not declared and tools:
        problems.append(
            "tools not declared in the manifest: " + ", ".join(sorted(tools))
        )
    commands = {name for spec in plugin.get_commands() for name in spec.names[:1]}
    declared_commands = set(manifest.provides.commands)
    if commands != declared_commands:
        problems.append(
            "commands differ from the manifest: declared "
            f"{sorted(declared_commands) or '[]'}, provided {sorted(commands) or '[]'}"
        )
    return problems


@dataclass(frozen=True)
class PluginInfo:
    """Everything known about one plugin without importing it."""

    plugin_id: str
    configured: InstalledPlugin | None
    lock: dict[str, str] | None

    @property
    def manifest(self) -> PluginManifest | None:
        return self.configured.manifest if self.configured else None

    def lines(self) -> list[str]:
        lines: list[str] = []
        if self.manifest is not None:
            lines.extend(self.manifest.summary_lines())
        else:
            lines.append(self.plugin_id)
            if self.configured is not None and self.configured.manifest_error:
                lines.append(f"  invalid manifest: {self.configured.manifest_error}")
            else:
                lines.append("  (no phoson-plugin.toml)")
        if self.configured is not None:
            state = "enabled" if self.configured.enabled else "disabled"
            lines.append(f"  status: {state}")
        else:
            lines.append("  status: not configured")
        if self.lock:
            lines.append(f"  source: {self.lock.get('source', '?')}")
            if self.lock.get("resolved_commit"):
                lines.append(f"  commit: {self.lock['resolved_commit']}")
            if self.lock.get("location"):
                lines.append(f"  location: {self.lock['location']}")
            if self.lock.get("installed_at"):
                lines.append(f"  installed: {self.lock['installed_at']}")
        return lines


def plugin_info(plugin_id: str, config: PhosonConfig) -> PluginInfo:
    """Describe a configured or installed plugin from its manifest and lockfile."""
    configured = next(
        (
            entry
            for entry in configured_plugins(config)
            if _matches(entry.spec, plugin_id)
        ),
        None,
    )
    lock = next((e for e in _load_lockfile() if e.get("id") == plugin_id), None)
    if configured is None and lock is None:
        raise PluginManagerError(f"Plugin not found: {plugin_id}")
    return PluginInfo(plugin_id=plugin_id, configured=configured, lock=lock)


# ─── Plugin configuration (`plugin config`) ──────────────────────────────────

_SECRET_MASK = "********"


@dataclass(frozen=True)
class ConfigRow:
    """One manifest config key with its effective value and where it comes from."""

    spec: ConfigField
    value: Any
    #: ``config`` (config.toml), ``env $NAME``, ``default`` or ``unset``.
    source: str

    def display_value(self) -> str:
        if self.source == "unset":
            return "-"
        if self.spec.secret:
            return _SECRET_MASK
        import json

        if isinstance(self.value, str):
            return self.value
        return json.dumps(self.value)


@dataclass(frozen=True)
class PluginConfigView:
    """A plugin's configuration as ``plugin config <id>`` shows it."""

    plugin_id: str
    manifest: PluginManifest | None
    values: dict[str, Any]
    rows: list[ConfigRow]
    problems: list[str]

    def lines(self) -> list[str]:
        if self.manifest is None or not self.manifest.config:
            lines = [f"{self.plugin_id}: no [config] schema in its manifest."]
            if self.values:
                lines.append("  Configured values (config.toml):")
                lines.extend(f"    {k} = {v!r}" for k, v in self.values.items())
            return lines
        lines = [f"{self.plugin_id} configuration:"]
        for row in self.rows:
            spec = row.spec
            flags = ", ".join(
                flag
                for flag, on in (("required", spec.required), ("secret", spec.secret))
                if on
            )
            head = f"  {spec.key} ({spec.type}{', ' + flags if flags else ''})"
            lines.append(f"{head} = {row.display_value()}  [{row.source}]")
            if spec.description:
                lines.append(f"      {spec.description}")
            if spec.choices:
                lines.append(
                    "      choices: " + ", ".join(repr(c) for c in spec.choices)
                )
            if spec.env:
                lines.append(f"      env: ${spec.env}")
        lines.extend(f"  problem: {problem}" for problem in self.problems)
        return lines


def _config_spec(config: PhosonConfig, plugin_id: str) -> tuple[list, int]:
    """The spec list holding *plugin_id* and its index there."""
    for specs in (config.plugins, config.disabled_plugins):
        for index, spec in enumerate(specs):
            if _matches(spec, plugin_id):
                return specs, index
    raise PluginManagerError(f"Configured plugin not found: {plugin_id}")


def _spec_values(spec: str | dict) -> dict[str, Any]:
    if isinstance(spec, dict) and isinstance(spec.get("config"), dict):
        return dict(spec["config"])
    return {}


def _store_values(
    config: PhosonConfig, specs: list, index: int, values: dict[str, Any]
) -> None:
    name = _plugin_name(specs[index])
    specs[index] = {"name": name, "config": values} if values else name
    save_config(config, only_fields={"plugins", "disabled_plugins"})


def _schema_field(manifest: PluginManifest | None, plugin_id: str, key: str):
    if manifest is None or not manifest.config:
        raise PluginManagerError(
            f"Plugin {plugin_id!r} declares no [config] schema; edit its inline "
            "table in ~/.phoson/config.toml instead."
        )
    fields = {f.key: f for f in manifest.config}
    if key not in fields:
        raise PluginManagerError(
            f"Unknown config key {key!r} for {plugin_id} "
            f"(expected one of: {', '.join(sorted(fields))})"
        )
    return fields[key]


def plugin_config_view(
    plugin_id: str,
    config: PhosonConfig,
    *,
    environ: dict[str, str] | None = None,
) -> PluginConfigView:
    """Effective configuration of a configured plugin, without importing it."""
    activate_plugin_paths()
    specs, index = _config_spec(config, plugin_id)
    spec = specs[index]
    values = _spec_values(spec)
    manifest, _error = _manifest_info(spec)
    env = os.environ if environ is None else environ
    rows: list[ConfigRow] = []
    problems: list[str] = []
    if manifest is not None and manifest.config:
        for field_spec in manifest.config:
            if field_spec.key in values:
                row = ConfigRow(field_spec, values[field_spec.key], "config")
            elif field_spec.env and field_spec.env in env:
                row = ConfigRow(
                    field_spec, env[field_spec.env], f"env ${field_spec.env}"
                )
            elif field_spec.default is not None:
                row = ConfigRow(field_spec, field_spec.default, "default")
            else:
                row = ConfigRow(field_spec, None, "unset")
            rows.append(row)
        try:
            manifest.resolve_config(values, environ=dict(env))
        except PluginManifestError as exc:
            problems = list(exc.problems) or [str(exc)]
    return PluginConfigView(plugin_id, manifest, values, rows, problems)


def set_plugin_config(
    plugin_id: str, key: str, raw_value: str, config: PhosonConfig
) -> tuple[Any, ConfigField]:
    """Validate *raw_value* against the manifest and store it in config.toml."""
    activate_plugin_paths()
    specs, index = _config_spec(config, plugin_id)
    manifest, _error = _manifest_info(specs[index])
    field_spec = _schema_field(manifest, plugin_id, key)
    try:
        value = field_spec.parse(raw_value)
    except ValueError as exc:
        raise PluginManagerError(f"{key}: {raw_value!r} {exc}") from None
    problem = field_spec.check(value)
    if problem:
        raise PluginManagerError(f"{key}: {problem}")
    values = _spec_values(specs[index])
    values[key] = value
    _store_values(config, specs, index, values)
    return value, field_spec


def unset_plugin_config(plugin_id: str, key: str, config: PhosonConfig) -> bool:
    """Remove *key* from the plugin's stored config; ``False`` if it was not set."""
    specs, index = _config_spec(config, plugin_id)
    values = _spec_values(specs[index])
    if key not in values:
        return False
    del values[key]
    _store_values(config, specs, index, values)
    return True
