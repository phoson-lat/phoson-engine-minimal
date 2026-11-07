"""Standalone-binary installs: layout, self-update and uninstall.

The installers (``scripts/phoson-installer.sh`` / ``.ps1``) unpack the
one-folder release bundle into a versioned layout and write a receipt::

    <root>/                         ~/.local/share/phoson  (Unix)
                                    %LOCALAPPDATA%\\Phoson  (Windows)
    ├── install-receipt.json        {"method": "binary", "version", "platform", ...}
    ├── current -> versions/<v>     symlink (Unix) / directory junction (Windows)
    └── versions/<v>/phoson-cli/    launcher + _internal/

``~/.local/bin/phoson-cli`` links to ``<root>/current/phoson-cli/phoson-cli``
(Unix); on Windows ``<root>\\current\\phoson-cli`` is on the user PATH. The
link target never changes, so an update only unpacks a new version directory
and swaps ``current`` — atomically on Unix (``rename`` of a symlink), and on
Windows without touching the running executable, which lives in the old
version directory.

Every download is verified against the release's ``SHA256SUMS`` before it is
unpacked, and archives are extracted with path checks (no absolute paths, no
``..``, no links escaping the destination).
"""

import os
import sys
import json
import shutil
import hashlib
import tarfile
import zipfile
import platform
import tempfile
import subprocess
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable

REPOSITORY = "phoson-lat/phoson-engine-minimal"
RELEASE_DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download"
RECEIPT_NAME = "install-receipt.json"
CHECKSUMS_NAME = "SHA256SUMS"
BUNDLE_DIR = "phoson-cli"
#: Versions kept after an update: the new one and the one it replaced.
KEEP_VERSIONS = 2

#: ``fetch(url) -> bytes``; injectable for tests (no network).
Fetch = Callable[[str], bytes]


class BinaryInstallError(Exception):
    """A user-actionable failure while updating or removing a binary install."""


# ── Platform and assets ──────────────────────────────────────────────────────


def platform_tag(system: str | None = None, machine: str | None = None) -> str:
    """``<os>-<arch>`` as used in release asset names (``darwin-arm64``)."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if os_name is None or arch is None:
        raise BinaryInstallError(f"No prebuilt binary for {system}/{machine}.")
    return f"{os_name}-{arch}"


def asset_name(tag: str) -> str:
    """Release archive name for a platform tag."""
    extension = "zip" if tag.startswith("windows-") else "tar.gz"
    return f"phoson-cli-{tag}.{extension}"


def launcher_name(tag: str) -> str:
    return "phoson-cli.exe" if tag.startswith("windows-") else "phoson-cli"


# ── Layout ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BinaryInstall:
    """A versioned binary install found on disk."""

    root: Path
    receipt: dict

    @property
    def version(self) -> str:
        return str(self.receipt.get("version", ""))

    @property
    def platform(self) -> str:
        return str(self.receipt.get("platform", ""))

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def current(self) -> Path:
        return self.root / "current"


def _read_receipt(root: Path) -> dict | None:
    try:
        # utf-8-sig: tolerate a BOM from a PowerShell-written receipt.
        data = json.loads((root / RECEIPT_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("method") != "binary":
        return None
    return data


def find_binary_install(executable: str | None = None) -> BinaryInstall | None:
    """The installer-managed layout this frozen executable runs from, if any.

    A binary downloaded by hand (no receipt, not under ``versions/<v>/``)
    returns ``None``: it is replaced manually, as before.
    """
    exe = Path(executable or sys.executable).resolve()
    # <root>/versions/<v>/phoson-cli/<launcher>
    try:
        bundle, version_dir, versions = exe.parent, exe.parent.parent, exe.parents[2]
    except IndexError:
        return None
    if bundle.name != BUNDLE_DIR or versions.name != "versions":
        return None
    root = versions.parent
    receipt = _read_receipt(root)
    if receipt is None or not version_dir.is_dir():
        return None
    return BinaryInstall(root=root, receipt=receipt)


def _write_receipt(install: BinaryInstall, **updates: object) -> None:
    receipt = {**install.receipt, **updates}
    target = install.root / RECEIPT_NAME
    tmp = target.with_name(f".{RECEIPT_NAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


# ── Download and verification ────────────────────────────────────────────────


def default_fetch(url: str) -> bytes:
    import httpx

    response = httpx.get(url, follow_redirects=True, timeout=120.0)
    if response.status_code == 404:
        raise BinaryInstallError(f"Not found: {url}")
    response.raise_for_status()
    return response.content


def _expected_sha256(checksums: str, name: str) -> str:
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise BinaryInstallError(f"{name} is not listed in {CHECKSUMS_NAME}.")


def download_verified(version: str, tag: str, fetch: Fetch = default_fetch) -> bytes:
    """Download a release archive and check it against ``SHA256SUMS``."""
    base = f"{RELEASE_DOWNLOAD}/v{version}"
    name = asset_name(tag)
    try:
        checksums = fetch(f"{base}/{CHECKSUMS_NAME}").decode("utf-8")
        payload = fetch(f"{base}/{name}")
    except BinaryInstallError as exc:
        raise BinaryInstallError(
            f"v{version} has no prebuilt binary for {tag} yet ({exc}). Try again later."
        ) from exc
    expected = _expected_sha256(checksums, name)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise BinaryInstallError(
            f"Checksum mismatch for {name}: expected {expected}, got {actual}. "
            "The download was discarded."
        )
    return payload


# ── Extraction ───────────────────────────────────────────────────────────────


def _safe_member(destination: Path, name: str) -> Path:
    target = (destination / name).resolve()
    if not (target == destination or destination in target.parents):
        raise BinaryInstallError(f"Unsafe path in archive: {name}")
    return target


def extract_bundle(payload: bytes, tag: str, destination: Path) -> Path:
    """Unpack an archive into *destination*; return the ``phoson-cli`` folder."""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(payload)
        handle.seek(0)
        if tag.startswith("windows-"):
            with zipfile.ZipFile(handle) as archive:
                for member in archive.namelist():
                    _safe_member(destination, member)
                archive.extractall(destination)
        else:
            with tarfile.open(fileobj=handle, mode="r:gz") as archive:
                for member in archive.getmembers():
                    _safe_member(destination, member.name)
                # "data" filter (3.12+): refuses absolute paths, links that
                # escape, device files; strips setuid bits.
                archive.extractall(destination, filter="data")
    bundle = destination / BUNDLE_DIR
    if not (bundle / launcher_name(tag)).is_file():
        raise BinaryInstallError("The archive does not contain a phoson-cli bundle.")
    return bundle


# ── Switching versions ───────────────────────────────────────────────────────


def _point_current(install: BinaryInstall, version_dir: Path) -> None:
    current = install.current
    if os.name == "nt":
        # Directory junctions need no privileges. Removing the junction does
        # not touch the running executable (it lives in the old version dir).
        if current.exists() or os.path.lexists(current):
            os.rmdir(current)
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(current), str(version_dir)],
            check=True,
            capture_output=True,
        )
        return
    tmp = current.with_name(f".current.{os.getpid()}")
    if os.path.lexists(tmp):
        tmp.unlink()
    os.symlink(Path("versions") / version_dir.name, tmp)
    os.replace(tmp, current)  # rename(2) replaces the link itself: atomic


def _prune_versions(install: BinaryInstall, keep: set[str]) -> None:
    if not install.versions_dir.is_dir():
        return
    entries = sorted(
        (p for p in install.versions_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    kept = 0
    for entry in entries:
        if entry.name in keep or kept < KEEP_VERSIONS:
            kept += 1
            continue
        shutil.rmtree(entry, ignore_errors=True)


def update_binary_install(
    install: BinaryInstall, version: str, fetch: Fetch = default_fetch
) -> Path:
    """Install *version* next to the current one and switch to it.

    The download is verified, unpacked into a staging directory, moved to
    ``versions/<version>`` and only then is ``current`` re-pointed. Returns
    the new version directory.
    """
    tag = install.platform or platform_tag()
    payload = download_verified(version, tag, fetch)
    install.versions_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=install.versions_dir))
    try:
        extract_bundle(payload, tag, staging)
        final = install.versions_dir / version
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = install.version
    _point_current(install, final)
    _write_receipt(install, version=version, platform=tag)
    _prune_versions(install, keep={version, previous})
    return final


# ── Uninstall ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BinaryUninstall:
    removed_root: bool
    removed_link: Path | None
    pending: str | None = None


def _remove_windows_path_entry(entry: str) -> None:
    """Drop *entry* from the user PATH (registry), as the installer added it."""
    if sys.platform != "win32":
        return
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        value, kind = winreg.QueryValueEx(key, "Path")
        target = entry.rstrip("\\")
        parts = [p for p in str(value).split(";") if p and p.rstrip("\\") != target]
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    finally:
        winreg.CloseKey(key)


def uninstall_binary(install: BinaryInstall) -> BinaryUninstall:
    """Remove the launcher link, PATH entry and the whole install root.

    On Windows the running executable cannot delete its own directory; the
    removal is handed to a detached ``cmd`` that waits for this process to
    exit.
    """
    link_value = install.receipt.get("bin_link")
    removed_link: Path | None = None
    if isinstance(link_value, str) and link_value:
        link = Path(link_value)
        if link.is_symlink() and str(install.root) in os.readlink(link):
            link.unlink()
            removed_link = link
    path_entry = install.receipt.get("path_entry")
    if os.name == "nt":
        if isinstance(path_entry, str) and path_entry:
            _remove_windows_path_entry(path_entry)
        script = f'ping -n 3 127.0.0.1 >nul & rmdir /s /q "{install.root}"'
        subprocess.Popen(  # noqa: S602 - fixed command, quoted path
            ["cmd", "/c", script],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
        return BinaryUninstall(
            removed_root=False,
            removed_link=removed_link,
            pending=f"{install.root} will be removed when phoson-cli exits",
        )
    shutil.rmtree(install.root)
    return BinaryUninstall(removed_root=True, removed_link=removed_link)


__all__ = [
    "BinaryInstall",
    "BinaryInstallError",
    "BinaryUninstall",
    "asset_name",
    "download_verified",
    "extract_bundle",
    "find_binary_install",
    "platform_tag",
    "uninstall_binary",
    "update_binary_install",
]
