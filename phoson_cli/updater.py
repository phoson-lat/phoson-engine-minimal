"""Self-update logic for the Phoson CLI.

Shared by the ``--self-update`` entry-point flag and the in-REPL
``/update`` command. The updater:

1. Reports the running version (``importlib.metadata``, ``"dev"`` when
   running from a source checkout).
2. Checks the latest release on PyPI (best effort — network failures
   degrade to a manual-instructions message).
3. Detects how the CLI was installed and runs the matching upgrade
   command (or explains why no upgrade applies).

The upgrade runs as an async subprocess so it never blocks the REPL's
event loop. After a successful upgrade the *running* process still has
the old code loaded — the user must restart the CLI.
"""

import os
import re
import sys
import json
import time
import asyncio
from pathlib import Path
from collections.abc import Callable, Awaitable

import httpx

PACKAGE = "phoson-engine-minimal"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_TIMEOUT = 10.0

# How often the startup check re-queries PyPI (IMPROVEMENTS.md E5). A
# successful check rewrites the cache, so with this interval the CLI does
# at most one PyPI round trip per day.
UPDATE_CHECK_INTERVAL = 86_400.0
# The startup check shares the explicit /update timeout (10 s). It runs as
# a background task that never blocks input or first paint; the deadline
# only bounds how long the check may hold a network connection.
STARTUP_CHECK_TIMEOUT = 10.0
# Hard deadline for the *upgrade subprocess* itself (uv tool upgrade / pip
# install -U). Without one a wedged network or a hung pip can freeze the
# REPL forever (F-38). Generous on purpose: a real install can download
# wheels, but it should never need more than a few minutes.
UPGRADE_TIMEOUT = 600.0
# Cache file holding the last check timestamp, its outcome, and — when an
# update is available — the latest version. Written atomically (tmp +
# rename) and best-effort: a failure to persist just means the next start
# re-checks.
LAST_UPDATE_CHECK = "last_update_check"


# ── Versions ──────────────────────────────────────────────────────────────────


def get_current_version() -> str:
    """Version of the installed distribution, or ``"dev"`` from source.

    The standalone binary (issue #93) does not ship package metadata, so
    the version is injected at build time (``phoson_cli._FROZEN_VERSION``);
    :func:`~phoson_cli._frozen.frozen_version` prefers that when present.
    """
    from importlib.metadata import PackageNotFoundError, version

    from phoson_cli._frozen import is_frozen, frozen_version

    try:
        current = version(PACKAGE)
    except PackageNotFoundError:
        current = "dev"
    if is_frozen():
        return frozen_version(current)
    return current


def _version_key(version: str) -> tuple:
    """Comparable key for ``X.Y.Z`` versions with optional pre-release suffix.

    A pre-release (``rc``/``alpha``/``beta``/``dev``/...) sorts below its
    release, so ``0.4.0rc1 < 0.4.0`` while ``0.4.0 < 0.5.0``.
    """
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", version.strip())
    if match is None:
        raise ValueError(f"Unparseable version: {version!r}")
    core, rest = match.group(1), match.group(2)
    pre = 0 if re.search(r"rc|alpha|beta|dev|pre", rest, flags=re.IGNORECASE) else 1
    parts = [int(piece) for piece in core.split(".")]
    return (*parts, pre)


def is_update_available(current: str, latest: str) -> bool:
    """True when ``latest`` is strictly newer than ``current``."""
    if current in {"", "dev"}:
        return True
    try:
        return _version_key(latest) > _version_key(current)
    except ValueError:
        # Unparseable version — be conservative and suggest an update.
        return latest != current


async def get_latest_version(timeout: float = CHECK_TIMEOUT) -> str | None:
    """Latest release on PyPI, or ``None`` when it cannot be determined."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(PYPI_JSON_URL)
            response.raise_for_status()
            return str(response.json()["info"]["version"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None


# ── Startup update check (IMPROVEMENTS.md E5) ───────────────────────────────


def _update_check_path() -> Path:
    """Cache file for the startup check: ``~/.phoson/last_update_check``."""
    home = os.environ.get("PHOSON_HOME", "~/.phoson")
    return Path(home).expanduser() / LAST_UPDATE_CHECK


def _read_update_check_cache(path: Path) -> dict | None:
    """Parse the check cache, or ``None`` when missing/unreadable/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_update_check_cache(path: Path, payload: dict) -> None:
    """Persist the check cache atomically; best-effort (never raises)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - read-only HOME etc.
        pass


def startup_check_due(path: Path, now: float | None = None) -> bool:
    """Whether a PyPI check is due (E5).

    Due when the cache is missing/corrupt, older than
    :data:`UPDATE_CHECK_INTERVAL`, or the last attempt did not succeed
    (no ``ok`` marker) — the interval is deliberately reset by failures
    so an offline user is retried on the next start without hammering
    PyPI. A successful "no update available" is *not* a failure: it
    sleeps for the full interval.
    """
    cache = _read_update_check_cache(path)
    if cache is None:
        return True
    last = cache.get("checked_at")
    if not isinstance(last, (int, float)):
        return True
    if (now if now is not None else time.time()) - last >= UPDATE_CHECK_INTERVAL:
        return True
    return not cache.get("ok")


def update_hint(latest_version: str) -> str:
    """The one-line, dim, non-blocking banner text for a newer release."""
    return f"⬆ v{latest_version} available — /update"


async def check_for_startup_update(
    path: Path | None = None,
    timeout: float = STARTUP_CHECK_TIMEOUT,
    *,
    now: float | None = None,
) -> str | None:
    """Non-blocking PyPI check for the startup banner (E5).

    Returns the latest version only when it is strictly newer than the
    running one — the front end renders :func:`update_hint` for it in a
    dim header/prompt slot (never blocks paint). Any failure (offline,
    bad payload) degrades to ``None``: no banner, no message, no retry
    loop. The cache records whether the attempt *succeeded* (``ok``):
    a failed check is retried on the next start, while a successful one
    — including "no update available" — waits out the full interval.
    """
    if path is None:
        path = _update_check_path()
    if not startup_check_due(path, now):
        return None
    latest = await get_latest_version(timeout=timeout)
    current = get_current_version()
    newer = latest is not None and is_update_available(current, latest)
    _write_update_check_cache(
        path,
        {
            "checked_at": time.time(),
            "ok": latest is not None,  # PyPI answered → full 24 h sleep
            "latest_version": latest if newer else None,
        },
    )
    return latest if newer else None


# ── Install-mode detection ────────────────────────────────────────────────────


class InstallMode:
    UV_TOOL = "uv-tool"
    UVX = "uvx"
    PIP = "pip"
    SOURCE = "source"
    FROZEN = "frozen"  # standalone PyInstaller binary (issue #93)
    UNKNOWN = "unknown"


def detect_install_mode() -> str:
    """Best-effort detection of how this CLI process was launched.

    Order matters: the frozen check comes first (a binary bundles a
    Python that also looks like a regular prefix), then the
    uv-tool/uvx prefix checks (their venvs also contain
    ``site-packages``), then the package path, then source.
    """
    from phoson_cli._frozen import is_frozen

    if is_frozen():
        return InstallMode.FROZEN

    prefix = Path(sys.prefix)
    exe = Path(sys.executable)
    pkg_dir = Path(__file__).resolve().parent

    # uv tool install: ~/.local/share/uv/tools/<pkg>/...
    if "uv" in prefix.parts and "tools" in prefix.parts:
        return InstallMode.UV_TOOL
    # uv tool run / uvx: ephemeral venvs under the uv cache
    # (e.g. ~/.cache/uv/archive-v0-*/...)
    if "uv" in prefix.parts and {".cache", "cache", "tmp"} & set(prefix.parts):
        return InstallMode.UVX

    # Installed via pip/uv pip into a site-packages dir (also matches an
    # editable install's target when the package is not in a source tree —
    # the source check below runs first and wins for checkouts).
    if "site-packages" in pkg_dir.parts:
        return InstallMode.PIP

    # Source checkout: the package lives next to a .git dir / pyproject.
    for parent in (pkg_dir, *pkg_dir.parents):
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return InstallMode.SOURCE

    if "site-packages" in prefix.parts or "site-packages" in exe.parts:
        return InstallMode.PIP

    return InstallMode.UNKNOWN


# ── Upgrade execution ─────────────────────────────────────────────────────────


def upgrade_command(mode: str) -> list[str] | None:
    """The upgrade command for an install mode, or None when none applies."""
    if mode == InstallMode.UV_TOOL:
        return ["uv", "tool", "upgrade", PACKAGE]
    if mode == InstallMode.PIP:
        return [sys.executable, "-m", "pip", "install", "-U", PACKAGE]
    return None  # source / uvx / unknown — handled as guidance, not a command


async def run_upgrade_command(
    command: list[str], timeout: float = UPGRADE_TIMEOUT
) -> tuple[int, str]:
    """Run an upgrade command, returning (returncode, combined output tail).

    A hard deadline bounds the whole subprocess (F-38): a wedged network or
    hung pip/uv can no longer freeze the REPL. On timeout the child is
    killed and ``(124, "timed out after {timeout}s")`` is returned — 124 is
    GNU ``timeout``'s conventional "killed by timeout" code.
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - already reaped
            pass
        await proc.wait()
        return 124, f"update command timed out after {timeout:.0f}s"
    output = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, output[-2000:]


def manual_hint(mode: str) -> str:
    """How a human can update themselves, per install mode."""
    if mode == InstallMode.FROZEN:
        return (
            "re-download the latest phoson-cli binary from the GitHub "
            "Releases page and replace the current executable"
        )
    if mode == InstallMode.UV_TOOL:
        return f"uv tool upgrade {PACKAGE}"
    if mode == InstallMode.PIP:
        return f"pip install -U {PACKAGE}"
    if mode == InstallMode.SOURCE:
        return "git pull && uv sync  (you are running from source)"
    if mode == InstallMode.UVX:
        return "no action needed — the next `uvx phoson-cli` uses the latest"
    return f"pip install -U {PACKAGE}  (or your package manager of choice)"


# ── Shared flow (used by the flag and the /update command) ───────────────────


async def _update_confirm(question: str) -> bool:
    """Async y/N prompt reusing prompt_toolkit (stays cooperative)."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    session: PromptSession[str] = PromptSession()
    try:
        with patch_stdout():
            answer = await session.prompt_async(f"{question} [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


async def perform_self_update(
    assume_yes: bool = False,
    confirm: Callable[[str], Awaitable[bool]] | None = None,
) -> str:
    """Check PyPI and, with confirmation, install the latest release.

    Args:
        assume_yes: Skip the interactive confirmation. Used by the
            explicit ``--self-update`` flag; the ``/update`` command
            always asks first.
        confirm: Optional async ``(prompt) -> bool``. Defaults to the
            prompt_toolkit y/N prompt so classic callers stay unchanged.

    Returns:
        One or more human-readable lines describing what happened.
    """
    current = get_current_version()
    lines = [f"Checking for updates... (current: {current})"]

    latest = await get_latest_version()
    if latest is None:
        mode = detect_install_mode()
        lines.append(
            "Could not check PyPI (offline or unreachable). "
            f"To update manually: {manual_hint(mode)}"
        )
        return "\n".join(lines)

    lines.append(f"Current: {current}  ·  Latest: {latest}")

    if not is_update_available(current, latest):
        lines.append(f"You're up to date ({latest}).")
        return "\n".join(lines)

    mode = detect_install_mode()
    if mode == InstallMode.SOURCE:
        lines.append(
            f"Update available, but you are running from source: {manual_hint(mode)}"
        )
        return "\n".join(lines)
    if mode == InstallMode.UVX:
        lines.append(
            "Update available, but this process runs via uvx (ephemeral) — "
            "the next invocation already uses the latest version."
        )
        return "\n".join(lines)

    command = upgrade_command(mode)
    if command is None:
        lines.append(
            f"Update available, but could not determine how to upgrade "
            f"automatically. Manual: {manual_hint(mode)}"
        )
        return "\n".join(lines)

    ask = confirm if confirm is not None else _update_confirm
    if not assume_yes and not await ask(
        f"Update {PACKAGE} {current} → {latest}? Run: {' '.join(command)}"
    ):
        lines.append("Update cancelled.")
        return "\n".join(lines)

    lines.append(f"Running: {' '.join(command)}")
    code, output = await run_upgrade_command(command)
    if code != 0:
        lines.append(f"Update failed (exit {code}):\n{output}")
        lines.append(f"Try manually: {manual_hint(mode)}")
        return "\n".join(lines)

    lines.append(f"✅ Updated to {latest} — restart the CLI to use it.")
    return "\n".join(lines)
