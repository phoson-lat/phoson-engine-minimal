"""OS sandbox for the ``run_code`` worker process.

The sandbox is what makes code mode safer than ``bash``: a sandboxed
script cannot open network connections, cannot write outside a private
scratch directory and cannot read the credential stores listed in
:data:`~phoson_agent.intents.SENSITIVE_HOME_PATHS`. Everything else it
wants to do to the outside world has to go through ``tools.*`` — and every
one of those calls passes the engine's permission gate.

Backends:

* **macOS** — ``sandbox-exec`` with a generated Seatbelt profile.
* **Linux** — ``bwrap`` (bubblewrap), when installed and usable (some
  distributions disable unprivileged user namespaces; that is probed once).
* **Windows / anything else** — none.

Modes (``CodeModeConfig.sandbox``): ``"auto"`` uses a backend when one is
available and otherwise runs unsandboxed with a note in the result;
``"required"`` refuses to run without one; ``"off"`` never sandboxes.
"""

import os
import sys
import shutil
import functools
import subprocess
from pathlib import Path
from dataclasses import dataclass

from phoson_agent.intents import SENSITIVE_HOME_PATHS

SANDBOX_MODES = ("auto", "required", "off")


class SandboxUnavailableError(RuntimeError):
    """``sandbox="required"`` but this platform has no usable backend."""


@dataclass(frozen=True)
class SandboxPlan:
    """The command to spawn and what protection it carries."""

    command: list[str]
    #: Backend name (``"seatbelt"``, ``"bwrap"``) or ``None`` when unsandboxed.
    backend: str | None
    #: Shown to the model when the run is not sandboxed under ``auto``.
    note: str | None = None


def _sensitive_paths(home: Path) -> list[Path]:
    return [home / rel for rel in SENSITIVE_HOME_PATHS]


def _real(path: Path | str) -> str:
    return os.path.realpath(os.fspath(path))


def _is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def _interpreter_dirs(base: list[str]) -> list[str]:
    """Directories the interpreter itself must keep reading.

    The standalone binary lives under ``~/.phoson`` (a masked path); when it
    is the worker interpreter its install directory is re-allowed.
    """
    if not base:
        return []
    exe = base[0]
    resolved = shutil.which(exe) or exe
    exe_dir = os.path.dirname(_real(resolved))
    # ``<prefix>/bin/python3`` needs ``<prefix>/lib`` too; a onedir binary
    # keeps its ``_internal`` folder next to the executable.
    if os.path.basename(exe_dir) in ("bin", "Scripts"):
        return [os.path.dirname(exe_dir)]
    return [exe_dir]


def _seatbelt_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(*, scratch: str, home: Path, reallow: list[str]) -> str:
    """Seatbelt profile: no network, writes only to ``scratch``, no secrets."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        "(allow file-write*",
        f"  (subpath {_seatbelt_quote(_real(scratch))})",
        '  (literal "/dev/null")',
        '  (literal "/dev/zero")',
        '  (literal "/dev/tty")',
        '  (regex #"^/dev/fd/"))',
    ]
    denied = [_real(p) for p in _sensitive_paths(home)]
    if denied:
        lines.append("(deny file-read* file-write*")
        lines.extend(f"  (subpath {_seatbelt_quote(p)})" for p in denied)
        lines[-1] += ")"
    keep = [d for d in reallow if any(_is_within(d, p) for p in denied)]
    if keep:
        lines.append("(allow file-read*")
        lines.extend(f"  (subpath {_seatbelt_quote(d)})" for d in keep)
        lines[-1] += ")"
    return "\n".join(lines) + "\n"


def bwrap_args(*, scratch: str, home: Path, reallow: list[str], cwd: str) -> list[str]:
    """bubblewrap arguments with the same policy as :func:`seatbelt_profile`."""
    args = [
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        scratch,
        scratch,
    ]
    denied: list[str] = []
    for path in _sensitive_paths(home):
        if path.is_dir():
            args += ["--tmpfs", str(path)]
            denied.append(_real(path))
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]
            denied.append(_real(path))
    for directory in reallow:
        if any(_is_within(directory, p) for p in denied):
            args += ["--ro-bind", directory, directory]
    args += ["--chdir", cwd]
    return args


@functools.lru_cache(maxsize=1)
def _bwrap_usable(binary: str) -> bool:
    """Probe once: bwrap exists but user namespaces may be disabled."""
    try:
        probe = subprocess.run(
            [binary, "--ro-bind", "/", "/", "--unshare-net", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def detect_backend() -> str | None:
    """The sandbox backend usable on this machine, or ``None``."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if sys.platform.startswith("linux"):
        binary = shutil.which("bwrap")
        if binary and _bwrap_usable(binary):
            return "bwrap"
    return None


def plan_command(
    base: list[str],
    *,
    mode: str,
    scratch: str,
    cwd: str,
    home: Path | None = None,
) -> SandboxPlan:
    """Wrap the worker command ``base`` in the sandbox ``mode`` asks for.

    Raises:
        SandboxUnavailableError: ``mode == "required"`` without a backend.
        ValueError: unknown ``mode``.
    """
    if mode not in SANDBOX_MODES:
        raise ValueError(f"sandbox must be one of {SANDBOX_MODES}, got {mode!r}")
    if mode == "off":
        return SandboxPlan(command=list(base), backend=None)
    home = home or Path.home()
    backend = detect_backend()
    reallow = _interpreter_dirs(base)
    if backend == "seatbelt":
        profile = seatbelt_profile(scratch=scratch, home=home, reallow=reallow)
        return SandboxPlan(
            command=["sandbox-exec", "-p", profile, *base], backend="seatbelt"
        )
    if backend == "bwrap":
        binary = shutil.which("bwrap") or "bwrap"
        args = bwrap_args(scratch=scratch, home=home, reallow=reallow, cwd=cwd)
        return SandboxPlan(command=[binary, *args, *base], backend="bwrap")
    if mode == "required":
        raise SandboxUnavailableError(
            "code mode requires a sandbox but none is available here "
            "(macOS sandbox-exec or Linux bwrap). Install bubblewrap, or set "
            "the code mode sandbox to 'auto'."
        )
    return SandboxPlan(
        command=list(base),
        backend=None,
        note=(
            "[not sandboxed: no OS sandbox is available on this machine, so the "
            "script ran with the same access as bash]"
        ),
    )


__all__ = [
    "SANDBOX_MODES",
    "SandboxPlan",
    "SandboxUnavailableError",
    "bwrap_args",
    "detect_backend",
    "plan_command",
    "seatbelt_profile",
]
