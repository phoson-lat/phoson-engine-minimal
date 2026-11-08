"""Optional dependencies ("extras") installed on demand.

Some features need packages that neither the standalone binary nor a plain
``phoson-cli`` install ships: speech-to-text (``moonshine-voice``), SSH
(``asyncssh``), Computer Use (``mss``/``Pillow``), the memory backends, and
the optional provider SDKs. ``/extras install <name>`` (or ``phoson-cli
extras install <name>``, or the installer's ``--with``) installs one of them
without ``pip`` and without touching the CLI's own environment:

- the packages go to ``~/.phoson/extras/<name>/`` with ``uv pip install
  --target`` (the binary has no ``pip``; a pinned, checksum-verified ``uv`` is
  downloaded once when none is on ``PATH``, see :mod:`.managed_uv`), built
  for the running Python version;
- the CLI's own packages are pinned as constraints and staged copies of them
  are pruned, so an extra never replaces a package the CLI ships;
- at startup :func:`activate_extras` appends each directory to ``sys.path``
  *after* the CLI's own paths, so bundled packages always win.

The catalog mirrors ``[project.optional-dependencies]`` in ``pyproject.toml``
(a test keeps them in sync). Python-environment users can still use
``pip install 'phoson-engine-minimal[<name>]'``; an extra found there is
reported as already available.
"""

import os
import sys
import json
import shutil
import tempfile
import importlib
import subprocess
import importlib.util
import importlib.metadata
from typing import Any
from pathlib import Path
from datetime import UTC, datetime
from dataclasses import field, dataclass
from collections.abc import Callable

from packaging.requirements import Requirement, InvalidRequirement

#: Written into each installed extra's directory.
RECORD_FILE = ".phoson-extra.json"

Runner = Callable[..., subprocess.CompletedProcess[str]]
Notify = Callable[[str], None]


class ExtrasError(Exception):
    """An extra could not be installed or removed."""


@dataclass(frozen=True)
class Extra:
    """One installable extra.

    ``requirements`` are pip requirement strings (environment markers are
    honoured: a requirement for another OS is skipped). ``modules`` must all
    be importable for the feature to work.
    """

    name: str
    description: str
    requirements: tuple[str, ...]
    modules: tuple[str, ...]
    #: ``pyproject.toml`` extras this one bundles (kept in sync by a test).
    pyproject: tuple[str, ...] = ()

    def applicable_requirements(self) -> list[str]:
        """The requirements whose markers match this machine."""
        out: list[str] = []
        for raw in self.requirements:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if req.marker is None or req.marker.evaluate():
                out.append(raw)
        return out


EXTRAS: tuple[Extra, ...] = (
    Extra(
        "stt",
        "Speech to text (voice input, Ctrl+O)",
        (
            "moonshine-voice>=0.1.5; sys_platform != 'darwin' or "
            "platform_machine != 'x86_64'",
        ),
        ("moonshine_voice",),
    ),
    Extra(
        "computeruse",
        "Computer Use: screenshots, mouse and keyboard",
        (
            "mss>=10.0.0",
            "Pillow>=10.0.0",
            "python-xlib>=0.33",
            "pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'",
            "jeepney>=0.9",
        ),
        ("mss", "PIL"),
        pyproject=(
            "computeruse",
            "computeruse-macos",
            "computeruse-windows",
            "computeruse-wayland",
        ),
    ),
    Extra("ssh", "SSH remote execution", ("asyncssh>=2.14.0",), ("asyncssh",)),
    Extra(
        "mcp",
        "Model Context Protocol servers",
        ("mcp>=1.0.0,<2.0.0",),
        ("mcp",),
    ),
    Extra(
        "memory",
        "Memory backends (Redis, Postgres, Qdrant)",
        ("redis>=5.0.0", "asyncpg>=0.29.0", "qdrant-client>=1.9.0"),
        ("redis", "asyncpg", "qdrant_client"),
    ),
    Extra(
        "checkpoint",
        "Postgres checkpoints",
        ("asyncpg>=0.29.0",),
        ("asyncpg",),
    ),
    Extra(
        "gemini", "Google Gemini provider", ("google-genai>=1.0.0",), ("google.genai",)
    ),
    Extra("mistral", "Mistral provider", ("mistralai>=1.0.0",), ("mistralai",)),
    Extra("aws", "AWS Bedrock provider", ("boto3>=1.35.0",), ("boto3",)),
)

_BY_NAME = {extra.name: extra for extra in EXTRAS}


def get_extra(name: str) -> Extra:
    """The catalog entry for *name* (case-insensitive), or :class:`ExtrasError`."""
    extra = _BY_NAME.get(name.strip().lower())
    if extra is None:
        known = ", ".join(sorted(_BY_NAME))
        raise ExtrasError(f"Unknown extra {name!r}. Available: {known}")
    return extra


def install_hint(name: str) -> str:
    """How to install extra *name*, for error messages."""
    return f"/extras install {name}  (or: phoson-cli extras install {name})"


# ── Locations ────────────────────────────────────────────────────────────────


def extras_dir() -> Path:
    home = Path(os.environ.get("PHOSON_HOME", "~/.phoson")).expanduser()
    return home / "extras"


def _python_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def read_record(name: str) -> dict[str, Any] | None:
    """The install record of extra *name*, or ``None`` when not installed."""
    path = extras_dir() / name / RECORD_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def activate_extras() -> list[Path]:
    """Make installed extras importable in this process.

    Appends each extra built for this Python version to ``sys.path``, after
    the CLI's own paths. Safe to call repeatedly; returns what it added.
    """
    added: list[Path] = []
    for extra in EXTRAS:
        record = read_record(extra.name)
        if record is None or record.get("python") != _python_tag():
            continue
        location = extras_dir() / extra.name
        text = str(location)
        if text not in sys.path:
            sys.path.append(text)
            added.append(location)
    if added:
        importlib.invalidate_caches()
    return added


# ── Status ───────────────────────────────────────────────────────────────────


@dataclass
class ExtraStatus:
    extra: Extra
    #: available, installed, missing, partial, stale, broken or unsupported
    state: str
    detail: str = ""
    missing_modules: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.state in {"available", "installed"}


def _module_location(module: str) -> Path | None:
    """Where *module* would be imported from, or ``None`` when it is absent."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    origin = spec.origin or next(iter(spec.submodule_search_locations or []), None)
    return Path(origin) if origin and origin not in {"built-in", "frozen"} else Path()


def extra_status(extra: Extra) -> ExtraStatus:
    """Whether *extra* is usable here, and from where."""
    if not extra.applicable_requirements():
        return ExtraStatus(extra, "unsupported", "not available on this platform")
    record = read_record(extra.name)
    if record is not None and record.get("python") != _python_tag():
        return ExtraStatus(
            extra,
            "stale",
            f"installed for Python {record.get('python')}; reinstall it",
        )
    locations = {module: _module_location(module) for module in extra.modules}
    missing = [module for module, where in locations.items() if where is None]
    if missing and len(missing) == len(extra.modules):
        return ExtraStatus(extra, "missing", missing_modules=missing)
    if missing:
        return ExtraStatus(
            extra, "partial", f"missing {', '.join(missing)}", missing_modules=missing
        )
    root = extras_dir() / extra.name
    if any(where is not None and _inside(where, root) for where in locations.values()):
        return ExtraStatus(extra, "installed", str(root))
    return ExtraStatus(extra, "available", "included")


def all_statuses() -> list[ExtraStatus]:
    return [extra_status(extra) for extra in EXTRAS]


# ── Install / remove ─────────────────────────────────────────────────────────


def _ensure_uv(notify: Notify) -> None:
    """Provision the private ``uv`` up front, reporting through *notify*.

    ``plugin_manager`` would otherwise print the download notice to stderr,
    which corrupts the full-screen TUI.
    """
    from ._frozen import is_frozen
    from .managed_uv import ManagedUvError, find_uv, ensure_uv

    if find_uv() is not None or not is_frozen():
        return
    try:
        ensure_uv(notify=notify)
    except ManagedUvError as exc:
        raise ExtrasError(str(exc)) from exc


def install_extra(
    name: str,
    *,
    runner: Runner = subprocess.run,
    notify: Notify = print,
) -> ExtraStatus:
    """Install extra *name* into ``~/.phoson/extras/<name>`` and activate it.

    Reinstalling replaces the previous copy only once the new one is ready.
    """
    from .plugin_manager import (
        PluginManagerError,
        _commit_directory,
        _host_constraints,
        _installer_command,
        _prune_host_copies,
        _host_distributions,
    )

    extra = get_extra(name)
    requirements = extra.applicable_requirements()
    if not requirements:
        raise ExtrasError(f"The {extra.name} extra is not available on this platform.")
    _ensure_uv(notify)

    root = extras_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    constraints = root / f"{staging.name}.constraints.txt"
    try:
        host = _host_distributions(exclude=(root,))
        pins = _host_constraints(host)
        constraints.write_text("\n".join(pins) + "\n", encoding="utf-8")
        try:
            command = _installer_command(
                staging,
                requirements[0],
                constraints if pins else None,
                *requirements[1:],
            )
        except PluginManagerError as exc:
            raise ExtrasError(str(exc)) from exc
        shown = ", ".join(req.split(";")[0].strip() for req in requirements)
        notify(f"Installing {extra.name}: {shown} …")
        result = runner(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ExtrasError(
                f"Installing {extra.name} failed:\n{_tail(detail) or 'no output'}"
            )
        _prune_host_copies(staging, host, keep="")
        (staging / ".lock").unlink(missing_ok=True)
        packages = sorted(
            f"{dist.metadata.get('Name')}=={dist.version}"
            for dist in importlib.metadata.distributions(path=[str(staging)])
            if dist.metadata.get("Name")
        )
        record = {
            "name": extra.name,
            "requirements": requirements,
            "packages": packages,
            "python": _python_tag(),
            "installed_at": datetime.now(UTC).isoformat(),
        }
        (staging / RECORD_FILE).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        final = root / extra.name
        _deactivate(final)
        _commit_directory(staging, final)
    finally:
        constraints.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    activate_extras()
    status = extra_status(extra)
    if status.ready:
        status = _verify_import(status)
    return status


def _verify_import(status: ExtraStatus) -> ExtraStatus:
    """Import the extra's modules now, so a broken install shows up here
    (a missing native library, a wheel for another Python) instead of when
    the feature is first used."""
    for module in status.extra.modules:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return ExtraStatus(
                status.extra,
                "broken",
                f"importing {module} failed: {type(exc).__name__}: {exc}",
            )
    return status


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _deactivate(location: Path) -> None:
    text = str(location)
    while text in sys.path:
        sys.path.remove(text)
    importlib.invalidate_caches()


def remove_extra(name: str) -> bool:
    """Delete extra *name*'s directory. Returns False when it was not installed.

    Modules already imported stay loaded until the CLI restarts.
    """
    extra = get_extra(name)
    location = extras_dir() / extra.name
    if not location.exists():
        return False
    _deactivate(location)
    shutil.rmtree(location)
    return True


# ── Presentation ─────────────────────────────────────────────────────────────

_STATE_LABELS = {
    "available": "ready",
    "installed": "installed",
    "missing": "not installed",
    "partial": "incomplete",
    "stale": "reinstall needed",
    "broken": "broken",
    "unsupported": "unsupported",
}


def format_status_table(statuses: list[ExtraStatus]) -> str:
    width = max(len(s.extra.name) for s in statuses)
    state_width = max(len(label) for label in _STATE_LABELS.values())
    lines = []
    for status in statuses:
        label = _STATE_LABELS[status.state]
        line = f"{status.extra.name:<{width}}  {label:<{state_width}}  "
        line += status.extra.description
        if status.detail and status.state not in {"available", "installed"}:
            line += f" ({status.detail})"
        lines.append(line.rstrip())
    return "\n".join(lines)


EXTRAS_HELP = """\
Usage: extras [list]               Show the optional features and their state
       extras install <name>...    Install them (into ~/.phoson/extras)
       extras remove <name>...     Delete an installed extra

Available: {names}
"""


def extras_help() -> str:
    return EXTRAS_HELP.format(names=", ".join(extra.name for extra in EXTRAS))


def run_extras_command(
    args: list[str],
    *,
    out: Notify = print,
    err: Notify = lambda msg: print(msg, file=sys.stderr),
    runner: Runner = subprocess.run,
) -> int:
    """``phoson-cli extras …`` (non-interactive). Returns the exit status."""
    if not args or args == ["list"]:
        out(format_status_table(all_statuses()))
        return 0
    command, *names = args
    if command in {"help", "-h", "--help"}:
        out(extras_help().rstrip())
        return 0
    if command not in {"install", "remove", "uninstall"} or not names:
        err(extras_help().rstrip())
        return 2
    status = 0
    for name in names:
        try:
            if command == "install":
                result = install_extra(name, runner=runner, notify=out)
                if result.ready:
                    out(f"✓ {result.extra.name} installed.")
                else:
                    err(
                        f"{result.extra.name} installed, but it is "
                        f"{_STATE_LABELS[result.state]}: {result.detail}"
                    )
                    status = 1
            elif remove_extra(name):
                out(
                    f"Removed {get_extra(name).name} (restart phoson-cli to unload it)."
                )
            else:
                out(f"{get_extra(name).name} is not installed in {extras_dir()}.")
        except ExtrasError as exc:
            err(str(exc))
            status = 1
    return status


__all__ = [
    "EXTRAS",
    "Extra",
    "ExtraStatus",
    "ExtrasError",
    "activate_extras",
    "all_statuses",
    "extra_status",
    "extras_dir",
    "format_status_table",
    "get_extra",
    "install_extra",
    "install_hint",
    "remove_extra",
    "run_extras_command",
]
