"""Suite-wide guard: never let a test touch the developer's real ``~/.phoson``.

Why this exists
---------------
The CLI resolves its config as ``Path("~/.phoson/config.toml").expanduser()``
(and ``Path.home()`` elsewhere). On POSIX that honours ``$HOME``, so tests
isolate themselves with ``monkeypatch.setenv("HOME", tmp_path)``. On Windows,
however, both ``ntpath.expanduser`` and ``pathlib`` ignore ``$HOME`` and use
``$USERPROFILE`` instead (CPython 3.12). The many ``setenv("HOME", ...)``
call sites therefore became no-ops on Windows: ``save_config`` kept writing the
developer's real ``config.toml`` (and its ``.bak``) while the tests still passed
because they read back the file ``save_config`` had just clobbered.

Two layers of defence keep the real home out of reach:

1. ``$HOME`` is made authoritative for ``~`` expansion on every platform (the
   ``_expanduser`` shim below). This restores the intended behaviour of the
   existing per-test ``HOME`` isolation on Windows too.
2. An autouse fixture hands every test its own fresh, throwaway home and points
   both ``HOME`` and ``USERPROFILE`` (plus ``HOMEDRIVE``/``HOMEPATH``) at it, so
   even a test that forgets to isolate writes only to temp space.

The import-time sandbox matters: ``PhosonConfig`` binds path defaults at class
creation time, so the environment must be redirected *before* ``phoson_cli`` is
imported. That is why this module does it at import time rather than in the
fixture alone.

Layer 3 is unrelated to ``~`` but belongs to the same "the suite must not be
able to hurt the developer's machine" theme. On Windows a test can take the
whole box down by naming the wrong process, and there are two ways to do it:
signalling a pid (``os.kill(pid, 0)``, the POSIX liveness probe, which Windows
routes to ``TerminateProcess``) and killing one by number (``taskkill /PID
<pid> /T /F``). Both are intercepted: a pid that is gone may already have been
recycled onto a critical system process, and terminating one of those makes
Windows bugcheck with ``CRITICAL_PROCESS_DIED``.
"""

import os
import atexit
import shutil
import signal
import tempfile
import subprocess
from pathlib import Path

import pytest

# ── Layer 1: make ``~`` follow $HOME (POSIX semantics) on every platform ──────

_REAL_EXPANDUSER = os.path.expanduser


def _expanduser(path):
    """``os.path.expanduser`` that honours ``$HOME`` for a bare ``~``.

    Windows ignores ``$HOME``; without this, every ``setenv("HOME", ...)`` in
    the tests would be ignored and ``~/.phoson`` would resolve to the real user
    profile. Non-``~`` paths and ``~user`` forms fall through unchanged.
    """
    home = os.environ.get("HOME")
    if home and not isinstance(path, bytes):
        if path == "~":
            return home
        if path[:2] in ("~/", "~\\"):
            return os.path.join(home, path[2:])
    return _REAL_EXPANDUSER(path)


os.path.expanduser = _expanduser

# ── Import-time sandbox so class-default paths are never the real home ────────

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="phoson-tests-home-"))
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)


def _point_home_at(target: Path) -> None:
    """Redirect every home-ish environment variable at ``target``."""
    os.environ["HOME"] = str(target)
    os.environ["USERPROFILE"] = str(target)
    drive, tail = os.path.splitdrive(str(target))
    os.environ["HOMEDRIVE"] = drive
    os.environ["HOMEPATH"] = tail or "\\"


_point_home_at(_SESSION_HOME)


# ── Layer 2: a fresh throwaway home per test ─────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_platform_home(monkeypatch):
    """Give each test its own home so no code path can reach the real one.

    The directory lives *outside* the test's ``tmp_path``: several tests assert
    that ``tmp_path`` is empty, so anything created inside it would be seen as a
    leftover artifact.
    """
    fake_home = Path(tempfile.mkdtemp(prefix="_home-", dir=_SESSION_HOME))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    drive, tail = os.path.splitdrive(str(fake_home))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail or "\\")
    return fake_home


# ── Layer 3: no destructive "liveness probes" on Windows ─────────────────────


@pytest.fixture(autouse=True)
def _no_destructive_os_kill_on_windows(monkeypatch):
    """Fail any Windows test that signals a process with ``os.kill``.

    ``os.kill(pid, 0)`` is the classic POSIX liveness probe, but it cannot
    probe on Windows: Python passes any signal other than
    ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` straight to ``TerminateProcess``, so
    the "probe" *kills* the target (the exit code becomes ``sig``) instead of
    asking whether it is alive. If that pid has been recycled onto a critical
    system process — ``csrss.exe``, ``winlogon.exe``, ``services.exe``,
    ``lsass.exe``, ``smss.exe`` — Windows bugchecks with
    ``CRITICAL_PROCESS_DIED``: a bluescreen, not a test failure. (``os.killpg``
    and ``os.getpgid`` cannot be used for this either; they simply do not exist
    on Windows.)

    Use :func:`phoson_agent.procutil.pid_alive` (``OpenProcess`` +
    ``GetExitCodeProcess``, no side effect) and
    :func:`phoson_agent.procutil.kill_process_tree` instead. Console control
    events are still allowed through, since they are the one signal Python
    relays rather than terminating with.
    """
    if os.name != "nt":
        return

    # Windows-only attributes: unreachable on POSIX, where they do not exist.
    ctrl_events = (signal.CTRL_C_EVENT, signal.CTRL_BREAK_EVENT)
    real_kill = os.kill

    def guarded_kill(pid: int, sig: int) -> None:
        if sig in ctrl_events:
            return real_kill(pid, sig)
        raise AssertionError(
            f"os.kill({pid}, {sig}) is destructive on Windows: non-CTRL signals "
            "go to TerminateProcess, so a recycled pid can bluescreen the "
            "machine. Use phoson_agent.procutil.pid_alive() / "
            "kill_process_tree() instead."
        )

    monkeypatch.setattr(os, "kill", guarded_kill)


def _taskkill_target_pid(argv: object) -> int | None:
    """The pid a ``taskkill`` command line names, or ``None`` for anything else.

    Only list-form argv is parsed: a shell string (``shell=True``) is opaque
    enough that guessing at it would be worse than not checking.
    """
    if isinstance(argv, (str, bytes)):
        return None
    try:
        parts = [os.path.basename(str(part)).lower() for part in argv]  # type: ignore[union-attr]
    except TypeError:
        return None
    if not parts or parts[0] not in ("taskkill", "taskkill.exe"):
        return None
    for index, part in enumerate(parts[1:], start=1):
        if part == "/pid" and index + 1 < len(parts):
            try:
                return int(parts[index + 1])
            except ValueError:
                return None
    return None


@pytest.fixture(autouse=True)
def _no_blind_taskkill_on_windows(monkeypatch):
    """Refuse a ``taskkill`` aimed by pid at something the suite cannot own.

    ``os.kill`` is not the only way a test can name a process it does not own:
    ``taskkill /PID <pid> /T /F`` is how Windows children get killed
    (``phoson_agent.procutil.kill_process_tree``), and it reaches the target *by
    number*. A pid that has been reaped is free to be recycled, so the kill can
    land on an unrelated process — or a critical system image, which takes the
    machine down with ``CRITICAL_PROCESS_DIED`` instead of failing the test.

    The production code refuses those targets itself; this trap exists so a
    test cannot bypass it with a raw ``subprocess.run(["taskkill", ...])``. A
    live, identifiable process that is not a critical image is allowed through:
    killing the suite's own children is exactly what the code under test does.
    """
    if os.name != "nt":
        return

    from phoson_agent.procutil import (
        _CRITICAL_PROCESS_IMAGES,
        pid_alive,
        windows_process_image,
    )

    real_run = subprocess.run
    real_popen = subprocess.Popen

    def refuse(argv: object) -> None:
        target = _taskkill_target_pid(argv)
        if target is None:
            return
        if not pid_alive(target):
            why = f"pid {target} is not running"
        else:
            image = windows_process_image(target)
            if image is None:
                why = f"pid {target} could not be identified"
            elif image in _CRITICAL_PROCESS_IMAGES:
                why = f"pid {target} is the critical system process {image!r}"
            else:
                return
        raise AssertionError(
            f"taskkill {argv!r} refused: {why}. Killing by pid on Windows can "
            "hit a recycled pid, and terminating a critical process "
            "bluescreens the machine. Kill the child you own through its "
            "handle (proc.kill()) instead."
        )

    def guarded_run(*args: object, **kwargs: object):
        if args:
            refuse(args[0])
        return real_run(*args, **kwargs)

    class GuardedPopen(real_popen):  # type: ignore[misc,valid-type]
        """``Popen`` that refuses a ``taskkill`` aimed at the wrong process.

        A subclass rather than a wrapper *function*: tests and libraries
        legitimately subclass ``subprocess.Popen`` (and isinstance-check
        against it), and that has to keep working.
        """

        def __init__(self, *args: object, **kwargs: object) -> None:
            if args:
                refuse(args[0])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
