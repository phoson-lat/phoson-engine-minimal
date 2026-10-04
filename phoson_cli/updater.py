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
import sys
import json
import math
import time
import signal
import asyncio
import tempfile
from pathlib import Path
from collections.abc import Callable, Awaitable

import httpx
from packaging.version import Version, InvalidVersion

from phoson_cli._version import PACKAGE, get_current_version

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


def _version_key(version: str) -> Version:
    """PEP 440 ordering, including epochs, pre/post releases and local versions."""
    return Version(version)


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
            latest = response.json()["info"]["version"]
            if not isinstance(latest, str):
                return None
            Version(latest)
            return latest
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
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
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            tmp = Path(stream.name)
            json.dump(payload, stream)
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - read-only HOME etc.
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
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
    if not isinstance(last, (int, float)) or not math.isfinite(last):
        return True
    age = (now if now is not None else time.time()) - last
    if age < 0 or age >= UPDATE_CHECK_INTERVAL:
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
        latest = (_read_update_check_cache(path) or {}).get("latest_version")
        if isinstance(latest, str) and is_update_available(
            get_current_version(), latest
        ):
            try:
                Version(latest)
                return latest
            except InvalidVersion:
                pass
        return None
    latest = await get_latest_version(timeout=timeout)
    current = get_current_version()
    newer = latest is not None and is_update_available(current, latest)
    _write_update_check_cache(
        path,
        {
            "checked_at": now if now is not None else time.time(),
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
    """Run without a shell; retain only an 8 KiB tail and reap on cancellation.

    POSIX children get their own process group so build helpers are stopped too.
    Windows cleanup currently covers the direct child only.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        return 127, f"Could not start {command[0]}: {exc}"

    tail = bytearray()

    async def drain() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(4096):
            tail.extend(chunk)
            del tail[:-8192]
        await proc.wait()

    async def stop() -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    reader = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(asyncio.shield(reader), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Shield cleanup from repeated Esc/Ctrl+C; do not return with a live child.
        cleanup = asyncio.create_task(stop())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
        cleanup.result()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, f"update command timed out after {timeout:.0f}s"
    output = tail.decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, output[-2000:]


async def get_installed_version() -> str | None:
    """Read disk metadata in a fresh interpreter, not the running CLI's cache."""
    code, output = await run_upgrade_command(
        [
            sys.executable,
            "-I",
            "-c",
            f"from importlib.metadata import version; print(version({PACKAGE!r}))",
        ],
        timeout=15.0,
    )
    if code != 0:
        return None
    try:
        Version(output)
    except InvalidVersion:
        return None
    return output


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
        return f"uvx --from {PACKAGE}@latest phoson-cli"
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


class UpdateResult(str):
    """String-compatible summary with explicit severity and process exit status."""

    exit_code: int
    level: str

    def __new__(cls, message: str, level: str = "info", exit_code: int = 0):
        result = super().__new__(cls, message)
        result.level = level
        result.exit_code = exit_code
        return result


async def perform_self_update(
    assume_yes: bool = False,
    confirm: Callable[[str], Awaitable[bool]] | None = None,
    notify: Callable[[str, str], None] | None = None,
) -> UpdateResult:
    """Check, ask, install and verify. Notifications contain plain text, not markup.

    Cancellation stops the installer but cannot roll back changes already made.
    The returned summary excludes progress messages to avoid duplicate UI output.
    """

    def progress(message: str) -> None:
        if notify is not None:
            notify("info", message)

    current = get_current_version()
    progress(f"Checking for updates · v{current}")
    latest = await get_latest_version()
    mode = detect_install_mode()
    if latest is None:
        return UpdateResult(
            f"Could not check PyPI. Try again later.\nManual: {manual_hint(mode)}",
            "warn",
            1,
        )
    if not is_update_available(current, latest):
        return UpdateResult(f"You're up to date (v{current}).")

    command = upgrade_command(mode)
    if command is None:
        reason = {
            InstallMode.SOURCE: "you are running from source",
            InstallMode.UVX: "uvx reuses cached versions",
            InstallMode.FROZEN: "standalone binaries need manual replacement",
        }.get(mode, "installation type not recognized")
        return UpdateResult(
            f"v{latest} available · {reason}.\nManual: {manual_hint(mode)}", "warn"
        )

    ask = confirm if confirm is not None else _update_confirm
    if not assume_yes and not await ask(f"Update phoson-cli {current} → {latest}?"):
        return UpdateResult("Update cancelled.")

    progress(f"Installing update · {current} → {latest}")
    try:
        code, output = await run_upgrade_command(command)
    except asyncio.CancelledError:
        if notify is not None:
            notify(
                "warn",
                "Update interrupted. Installation may be incomplete; "
                f"repair with: {manual_hint(mode)}",
            )
        raise
    if code != 0:
        # Keep routine output quiet; show a short diagnostic only on failure.
        detail = "\n".join(output.splitlines()[-6:])
        return UpdateResult(
            f"Update failed (exit {code}).\n{detail}\n"
            f"Try manually: {manual_hint(mode)}",
            "error",
            1,
        )

    installed = await get_installed_version()
    if installed is None:
        return UpdateResult(
            "Installer finished, but the installed version could not be verified.\n"
            "Restart the CLI and check phoson-cli --version.",
            "warn",
            1,
        )
    if not is_update_available(current, installed):
        return UpdateResult(
            f"No version change (v{installed}); v{latest} is available on PyPI.\n"
            "Check your package manager's version constraints and index.",
            "warn",
        )
    if is_update_available(installed, latest):
        return UpdateResult(
            f"Updated to v{installed}; PyPI has v{latest}.\n"
            "Your package manager may have version constraints. Restart the CLI.",
            "warn",
        )
    return UpdateResult(f"Updated to v{installed} — restart the CLI to use it.")


def print_update_result(console, result: str) -> None:
    """Render literal text with Rich (never interpret installer output as markup)."""
    from rich.text import Text

    level = getattr(result, "level", "info")
    style = {"info": "green", "warn": "yellow", "error": "red"}[level]
    console.print(Text(str(result), style=style))
