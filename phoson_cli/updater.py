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

import re
import sys
import asyncio
from pathlib import Path

import httpx

PACKAGE = "phoson-engine-minimal"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_TIMEOUT = 10.0


# ── Versions ──────────────────────────────────────────────────────────────────


def get_current_version() -> str:
    """Version of the installed distribution, or ``"dev"`` from source."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "dev"


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


# ── Install-mode detection ────────────────────────────────────────────────────


class InstallMode:
    UV_TOOL = "uv-tool"
    UVX = "uvx"
    PIP = "pip"
    SOURCE = "source"
    UNKNOWN = "unknown"


def detect_install_mode() -> str:
    """Best-effort detection of how this CLI process was launched.

    Order matters: the uv-tool/uvx prefix checks come first (their venvs
    also contain ``site-packages``), then the package path, then source.
    """
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


async def run_upgrade_command(command: list[str]) -> tuple[int, str]:
    """Run an upgrade command, returning (returncode, combined output tail)."""
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout_b, _ = await proc.communicate()
    output = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, output[-2000:]


def manual_hint(mode: str) -> str:
    """How a human can update themselves, per install mode."""
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


async def perform_self_update(assume_yes: bool = False) -> str:
    """Check PyPI and, with confirmation, install the latest release.

    Args:
        assume_yes: Skip the interactive confirmation. Used by the
            explicit ``--self-update`` flag; the ``/update`` command
            always asks first.

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

    if not assume_yes and not await _update_confirm(
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
