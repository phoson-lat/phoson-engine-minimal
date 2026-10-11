"""Cross-platform process probes and termination.

Two operations look portable but are not, and the difference is dangerous
enough that nothing in the codebase should call the raw primitives:

Liveness
    ``os.kill(pid, 0)`` is a *probe* on POSIX (it delivers no signal), but on
    Windows every signal other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` is
    handed straight to ``TerminateProcess`` — the exit code becomes ``sig``.
    So on Windows ``os.kill(pid, 0)`` does not ask "is it alive?", it *kills*
    it. That is bad enough for a pid we own, and a bluescreen when the pid has
    been recycled and now belongs to a critical system process
    (``csrss.exe``, ``winlogon.exe``, ``services.exe``, ``lsass.exe``,
    ``smss.exe``): terminating one of those makes Windows bugcheck with
    ``CRITICAL_PROCESS_DIED``. Windows is probed with ``OpenProcess`` +
    ``GetExitCodeProcess`` instead, which has no side effect.

Process groups
    ``os.killpg``, ``os.getpgid``, ``os.getpgrp`` and ``os.setsid`` do not
    exist on Windows at all (``AttributeError``, not ``OSError``, so it slips
    past ``except OSError``). There is no process-group concept there: a child
    is *not* detached (``start_new_session`` is ignored, see
    :func:`session_kwargs`) and is killed with ``taskkill /PID <pid> /T /F``.

Killing by pid
    The same "a pid is only a number" hazard applies to every kill, not just
    to the liveness probe: a pid that has already been reaped is free to be
    recycled, so a killer that fires on it hits an unrelated process — and
    ``taskkill /T /F`` takes the whole descendant tree with it. Nothing here
    signals a pid without first confirming it is still running and, on
    Windows, that it is not one of the ``_CRITICAL_PROCESS_IMAGES``. Kill the
    child you own through its handle (``proc.kill()``) where possible: a handle
    keeps the process object alive, so the pid cannot be recycled under you.

Use :func:`pid_alive`, :func:`process_group_alive`, :func:`kill_process_tree`,
:func:`kill_process_group` and :func:`session_kwargs` rather than the raw
``os`` calls, and start children with ``start_new_session=True`` on POSIX
(:func:`session_kwargs`) so that the group id is safe to signal — signalling
the wrong group can reach the host process (the test runner included).
"""

import os
import signal
import subprocess
from typing import TypedDict

# ── Win32 constants (processthreadsapi.h / winbase.h) ────────────────────

#: ``PROCESS_QUERY_LIMITED_INFORMATION`` (Vista+) is enough to read the exit
#: code and also works for processes owned by other users, unlike the heavier
#: ``PROCESS_QUERY_INFORMATION``.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: ``STILL_ACTIVE``: ``GetExitCodeProcess`` reports this while it runs.
_STILL_ACTIVE = 259

#: ``taskkill`` is a fixed, tiny call: never let it outlive the caller.
_TASKKILL_TIMEOUT_SECONDS = 2

#: Processes whose death makes Windows bugcheck (``CRITICAL_PROCESS_DIED``,
#: 0xEF) — it cannot run at all without them. A pid is just a number, so a
#: recycled pid can name one of these; the image is checked before any kill by
#: pid, because terminating one of these *is* the bluescreen this module exists
#: to prevent.
_CRITICAL_PROCESS_IMAGES = frozenset(
    {
        "system",
        "idle",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "lsaiso.exe",
        "memcompression",
        "registry",
    }
)


def _has_process_groups() -> bool:
    """True where the ``os.killpg`` family exists (POSIX, but not WASI).

    All four symbols are POSIX-only, so the check is on the whole family: the
    trap this module exists to close is that a missing ``os`` attribute raises
    ``AttributeError``, which the ``except OSError`` blocks here would *not*
    catch.
    """
    if os.name == "nt":
        return False
    needed = ("killpg", "getpgid", "getpgrp")
    return all(hasattr(os, name) for name in needed) and hasattr(signal, "SIGKILL")


# ``os.getpgid`` / ``os.killpg`` / ``signal.SIGKILL`` are POSIX-only, and here
# they are *looked up* at each use rather than referenced: a missing attribute
# would otherwise be an ``AttributeError`` (not an ``OSError``, so it slips past
# the handlers in this module — the #269 failure) and reading the attribute is
# also how a caller's monkeypatch — and a platform where it never existed — is
# honoured. Every call site checks :func:`_has_process_groups` first.


def _getpgid(pid: int) -> int:
    """``os.getpgid`` — POSIX only, so the caller checks the platform first."""
    return getattr(os, "getpgid")(pid)


def _killpg(pgid: int, sig: int) -> None:
    """``os.killpg`` — POSIX only. ``sig=0`` asks without signalling."""
    getattr(os, "killpg")(pgid, sig)


def _sigkill() -> int:
    """``signal.SIGKILL`` — POSIX only, so the caller checks the platform first."""
    return getattr(signal, "SIGKILL")


def pid_alive_windows(pid: int) -> bool:
    """Windows liveness probe via the Win32 API (no ``os.kill`` side effect)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # No such pid (or no rights to query it): treat as gone. ``pid_alive``
        # is only ever a hint, so failing closed is the safe direction.
        return False
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == _STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int | None) -> bool:
    """True when process ``pid`` is still running (best effort, no side effect).

    Safe on every platform: it never signals the process, so it cannot kill a
    recycled pid on Windows (see the module docstring).
    """
    if not pid or pid < 0:
        return False
    if os.name == "nt":
        return pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user: it is alive.
        return True
    except OSError:
        return False


def windows_process_image(pid: int) -> str | None:
    """Lowercased executable basename behind ``pid``, or ``None`` if unknown.

    Read with the same handle-based Win32 call family as the liveness probe, so
    it has no side effect. ``None`` means "could not tell" (no such pid, or the
    rights to query it are missing) and must be treated as "do not kill".
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        if not ok:
            return None
        return os.path.basename(buffer.value).lower() or None
    finally:
        kernel32.CloseHandle(handle)


def _kill_by_pid_refusal(pid: int) -> str | None:
    """Why ``pid`` must not be killed *by number* on Windows, else ``None``.

    Killing by pid is the second half of the hazard this module documents: a
    pid is only a number, so either it is already gone (and free to be recycled
    the instant we look away) or it has *been* recycled and now names something
    else entirely. Both cases are refused before ``taskkill`` is allowed near
    it, and a critical system image is refused outright.
    """
    if not pid_alive(pid):
        return f"pid {pid} is not running (nothing of ours to kill)"
    image = windows_process_image(pid)
    if image is None:
        return f"pid {pid} could not be identified"
    if image in _CRITICAL_PROCESS_IMAGES:
        return f"pid {pid} is the critical system process {image!r}"
    return None


def process_group_of(pid: int | None) -> int | None:
    """The process-group id of ``pid``, or ``None`` where there is no such thing.

    ``os.getpgid`` is POSIX-only, so callers cannot tell "this platform has no
    process groups" from "the query failed" by catching exceptions: both are
    reported as ``None`` and the caller falls back to the pid.

    Callers must only use the result for a child they deliberately detached
    (:func:`session_kwargs`). For any other process ``getpgid`` answers with
    *the caller's own group*, which is the group :func:`kill_process_group`
    must never be handed.
    """
    if not pid or pid < 0:
        return None
    if not _has_process_groups():
        return None
    try:
        return _getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def process_group_alive(pgid: int | None) -> bool:
    """True when process group ``pgid`` still has at least one member.

    POSIX probes the group with ``os.killpg(pgid, 0)``, which delivers no
    signal and so is safe. Windows has no process groups: callers track the
    child pid there and this degrades to :func:`pid_alive`.
    """
    if not pgid or pgid < 0:
        return False
    if not _has_process_groups():
        return pid_alive(pgid)
    try:
        _killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The group exists but belongs to another user: it is alive.
        return True
    except OSError:
        return False


def kill_process_group(pgid: int | None) -> None:
    """Kill every member of process group ``pgid``; best effort, never raises.

    The group is signalled directly instead of being resolved from a pid
    (:func:`kill_process_tree` needs the leader alive to look it up), because
    reaching the grandchildren a leader may already have left behind is the
    whole point of a group kill.

    Windows has no process groups, so this falls back to
    :func:`kill_process_tree` on the pid the caller tracks there. Like the
    POSIX branch of ``kill_process_tree``, it is only safe for a group the
    caller owns — never pass ``os.getpgrp()``.

    The ``pgid`` must come from :func:`process_group_of` on a child the caller
    deliberately detached with :func:`session_kwargs`, captured *while that
    child was alive*: a group id is a pid, so a value kept past the process's
    death is back in the pool.
    """
    if not pgid or pgid < 0:
        return
    if not process_group_alive(pgid):
        # An empty group has nothing to kill, and probing first keeps the
        # "never signal a number that may have been recycled" rule uniform.
        return
    if not _has_process_groups():
        kill_process_tree(pgid)
        return
    try:
        _killpg(pgid, _sigkill())
    except (ProcessLookupError, PermissionError, OSError):
        pass


class SessionKwargs(TypedDict, total=False):
    """Keyword arguments for ``Popen``/``create_subprocess_*``.

    ``total=False`` because the options do not exist on Windows: an empty dict
    is a valid value, and ``**session_kwargs()`` must stay type-correct inside
    a call that also passes its own keywords.
    """

    start_new_session: bool


def session_kwargs(*, new_session: bool = True) -> SessionKwargs:
    """``Popen``/``create_subprocess_*`` kwargs that detach the child on POSIX.

    On Windows ``start_new_session`` and ``process_group`` are accepted and
    **silently ignored** (CPython's Windows ``_execute_child`` names them
    ``unused_start_new_session``/``unused_process_group``): they do not map to
    ``CREATE_NEW_PROCESS_GROUP``, which is only ever requested explicitly
    through ``creationflags``. So this returns ``{}`` there — not because the
    option would be harmful, but because it would be a lie to claim the child
    was detached: Windows children are killed by pid (``taskkill /T``) rather
    than by group, which is why :func:`kill_process_tree` verifies the pid.
    """
    if not new_session or os.name == "nt":
        return {}
    return {"start_new_session": True}


def kill_process_tree(pid: int | None) -> None:
    """Kill ``pid`` and its descendants; best effort, never raises.

    POSIX: ``SIGKILL`` to the process group, which is only correct when the
    child was started with :func:`session_kwargs` (otherwise the group is the
    caller's own and this would signal itself). Windows: ``taskkill /T /F``,
    which walks the descendant tree because ``os.killpg`` does not exist.

    A pid is only a number, so neither branch fires unless the process is still
    running *and* (on Windows) is not a critical system image: a pid that has
    been reaped is the one a killer must never use, because the number is then
    free to have been recycled onto an unrelated process — the bluescreen
    scenario from the module docstring. Callers keep ``proc.kill()`` for the
    child itself, which goes through the open process handle and is therefore
    immune to recycling.
    """
    if not pid or pid < 0:
        return
    if os.name == "nt":
        if _kill_by_pid_refusal(pid) is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TASKKILL_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # No taskkill, or it hung: the caller still has ``proc.kill()``.
            pass
        return
    if not _has_process_groups():
        # Neither a process group nor taskkill to fall back on: doing nothing
        # keeps the "never raises" promise, and the caller keeps proc.kill().
        return
    if not pid_alive(pid):
        # ``os.getpgid`` below would raise ``ProcessLookupError`` for a dead
        # pid anyway, so this only makes the existing no-op explicit — and
        # keeps the pid-recycling window closed on both platforms.
        return
    try:
        _killpg(_getpgid(pid), _sigkill())
    except (ProcessLookupError, PermissionError, OSError):
        pass


__all__ = [
    "pid_alive",
    "SessionKwargs",
    "process_group_of",
    "pid_alive_windows",
    "kill_process_tree",
    "session_kwargs",
    "kill_process_group",
    "process_group_alive",
    "windows_process_image",
]
