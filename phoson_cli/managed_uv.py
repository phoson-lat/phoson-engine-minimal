"""A private copy of ``uv`` for installing plugins from the standalone binary.

The standalone binary has no Python interpreter or ``pip`` to hand to an
installer, so plugin installs need ``uv``. Users who installed the binary
usually do not have it, so when none is on ``PATH`` a pinned ``uv`` release is
downloaded once from Astral's GitHub releases into ``~/.phoson/bin``.

The download is pinned to :data:`UV_VERSION` and verified against SHA-256
digests embedded here (taken from that release's ``.sha256`` assets), so a
compromised or changed upstream asset is refused rather than run.
``PHOSON_NO_MANAGED_UV=1`` disables the download.
"""

import io
import os
import sys
import shutil
import hashlib
import tarfile
import zipfile
import platform
import tempfile
from pathlib import Path
from collections.abc import Callable

UV_VERSION = "0.13.1"

_RELEASE_URL = "https://github.com/astral-sh/uv/releases/download/{version}/{asset}"

#: Release asset and its SHA-256 per (sys.platform prefix, machine).
_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        "uv-aarch64-apple-darwin.tar.gz",
        "cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2",
    ),
    ("darwin", "x86_64"): (
        "uv-x86_64-apple-darwin.tar.gz",
        "bb7b805bdcf3d0bc8c74ceb94569e6b3e8aa35bd492acdbb9d46bf46d9d4ff6b",
    ),
    ("linux", "x86_64"): (
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220",
    ),
    ("linux", "arm64"): (
        "uv-aarch64-unknown-linux-gnu.tar.gz",
        "9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14",
    ),
    ("win32", "x86_64"): (
        "uv-x86_64-pc-windows-msvc.zip",
        "3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672",
    ),
}

_MACHINES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

Downloader = Callable[[str], bytes]


class ManagedUvError(RuntimeError):
    """uv is unavailable and could not be provisioned."""


def _phoson_home() -> Path:
    return Path(os.environ.get("PHOSON_HOME", "~/.phoson")).expanduser()


def managed_uv_path() -> Path:
    """Where the private copy lives (``~/.phoson/bin/uv``)."""
    name = "uv.exe" if sys.platform == "win32" else "uv"
    return _phoson_home() / "bin" / name


def find_uv() -> str | None:
    """``uv`` on ``PATH``, else the private copy if it was provisioned."""
    on_path = shutil.which("uv")
    if on_path:
        return on_path
    managed = managed_uv_path()
    return str(managed) if managed.is_file() else None


def platform_asset() -> tuple[str, str] | None:
    """The release asset (name, sha256) for this system, or ``None``."""
    system = "win32" if sys.platform == "win32" else sys.platform
    for prefix in ("darwin", "linux", "win32"):
        if system.startswith(prefix):
            system = prefix
            break
    machine = _MACHINES.get(platform.machine().lower())
    if machine is None:
        return None
    return _ASSETS.get((system, machine))


def _download(url: str) -> bytes:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _extract_uv(asset: str, data: bytes) -> bytes:
    """The ``uv`` executable's bytes from a release archive."""
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.rsplit("/", 1)[-1] == "uv.exe":
                    return archive.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.isfile() and member.name.rsplit("/", 1)[-1] == "uv":
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
    raise ManagedUvError(f"{asset} does not contain a uv executable")


def install_managed_uv(
    *,
    download: Downloader = _download,
    notify: Callable[[str], None] | None = None,
) -> str:
    """Download, verify and install the pinned ``uv``; return its path."""
    asset = platform_asset()
    if asset is None:
        raise ManagedUvError(
            f"No uv build for {sys.platform}/{platform.machine()}; install uv "
            "yourself (https://docs.astral.sh/uv/)."
        )
    name, expected = asset
    url = _RELEASE_URL.format(version=UV_VERSION, asset=name)
    if notify is not None:
        notify(f"Downloading uv {UV_VERSION} to install plugins ({url})")
    try:
        data = download(url)
    except Exception as exc:  # network, TLS, HTTP status
        raise ManagedUvError(f"Could not download uv from {url}: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ManagedUvError(
            f"Checksum mismatch for {name} (expected {expected}, got {actual}); "
            "refusing to install it."
        )
    binary = _extract_uv(name, data)

    target = managed_uv_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".uv-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(binary)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return str(target)


def ensure_uv(
    *,
    download: Downloader = _download,
    notify: Callable[[str], None] | None = None,
) -> str:
    """Path to a usable ``uv``, provisioning the private copy if needed."""
    found = find_uv()
    if found:
        return found
    if os.environ.get("PHOSON_NO_MANAGED_UV"):
        raise ManagedUvError(
            "uv is not on PATH and PHOSON_NO_MANAGED_UV is set; install uv "
            "(https://docs.astral.sh/uv/) to manage plugins."
        )
    return install_managed_uv(download=download, notify=notify)
