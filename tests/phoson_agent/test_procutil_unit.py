"""Unit tests for :mod:`phoson_agent.procutil` (issue #269 follow-up).

The module exists because two "portable" primitives are not portable at all:
``os.kill(pid, 0)`` is a liveness *probe* on POSIX but ``TerminateProcess`` on
Windows, and the ``os.killpg`` family does not exist there. These tests pin the
dispatch and the command line each platform produces.

Safety: nothing here signals a process the test does not own. ``os.killpg`` /
``os.getpgid`` / ``subprocess.run`` (and the Win32 probe) are monkeypatched, and
the only real signal-free probes are against the test's own pid and a child it
has already reaped — so the file is safe on Windows, where a stray
``os.kill(pid, 0)`` can kill a recycled pid and bluescreen the machine.
"""

import os
import sys
import signal
import subprocess

import pytest

from phoson_agent import procutil
from phoson_agent.procutil import (
    pid_alive,
    session_kwargs,
    process_group_of,
    kill_process_tree,
    pid_alive_windows,
    kill_process_group,
    process_group_alive,
    windows_process_image,
)

POSIX = os.name != "nt"


def _forbid(name: str):
    """A stand-in that fails the test if the code under test calls it."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name} must not be reached here")

    return explode


# ── pid_alive ────────────────────────────────────────────────────────────────


class TestPidAlive:
    def test_own_pid_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    @pytest.mark.parametrize("pid", [None, 0, -1])
    def test_falsy_or_negative_pid_is_not_alive(self, pid: int | None) -> None:
        assert pid_alive(pid) is False

    def test_reaped_child_is_not_alive(self) -> None:
        # The child has been waited on: gone on POSIX, exited on Windows
        # (GetExitCodeProcess reports something other than STILL_ACTIVE).
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert pid_alive(proc.pid) is False

    def test_windows_dispatches_to_the_win32_probe(self, monkeypatch) -> None:
        seen: list[int] = []

        def fake_probe(pid: int) -> bool:
            seen.append(pid)
            return True

        monkeypatch.setattr(procutil, "pid_alive_windows", fake_probe)
        monkeypatch.setattr(os, "name", "nt")
        assert pid_alive(4242) is True
        assert seen == [4242]


@pytest.mark.skipif(not POSIX, reason="os.kill(pid, 0) is the POSIX probe")
class TestPidAlivePosix:
    def test_probe_is_signal_zero(self, monkeypatch) -> None:
        seen: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: seen.append((pid, sig)))
        assert pid_alive(4242) is True
        assert seen == [(4242, 0)]

    def test_process_lookup_error_means_gone(self, monkeypatch) -> None:
        def missing(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", missing)
        assert pid_alive(4242) is False

    def test_permission_error_means_alive(self, monkeypatch) -> None:
        def other_user(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(os, "kill", other_user)
        assert pid_alive(4242) is True

    def test_other_os_error_means_gone(self, monkeypatch) -> None:
        def broken(pid: int, sig: int) -> None:
            raise OSError(22, "invalid argument")

        monkeypatch.setattr(os, "kill", broken)
        assert pid_alive(4242) is False


@pytest.mark.skipif(POSIX, reason="Win32 only")
class TestPidAliveWindows:
    def test_own_pid_is_alive(self) -> None:
        assert pid_alive_windows(os.getpid()) is True

    def test_pid_zero_is_not_a_process(self) -> None:
        # OpenProcess refuses the idle-process pseudo pid, so it fails closed.
        assert pid_alive_windows(0) is False


# ── session_kwargs ───────────────────────────────────────────────────────────


class TestSessionKwargs:
    def test_posix_detaches_the_child(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "name", "posix")
        assert session_kwargs() == {"start_new_session": True}

    def test_posix_opt_out(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "name", "posix")
        assert session_kwargs(new_session=False) == {}

    def test_windows_never_passes_it(self, monkeypatch) -> None:
        # Windows ignores ``start_new_session``/``process_group`` (CPython names
        # them ``unused_*`` there), so the option would be a false claim of
        # detachment: Windows children are killed by pid, not by group.
        monkeypatch.setattr(os, "name", "nt")
        assert session_kwargs() == {}
        assert session_kwargs(new_session=False) == {}


# ── kill_process_tree ────────────────────────────────────────────────────────


def _raiser(error: type[Exception]):
    """A stand-in that raises ``error()`` when the code under test calls it."""

    def explode(*args: object, **kwargs: object) -> None:
        raise error()

    return explode


def _stub_kill_gates(
    monkeypatch,
    *,
    alive: bool = True,
    image: str | None = "cmd.exe",
) -> None:
    """Make the pid-based kill gates see a live, non-critical target.

    ``kill_process_tree`` refuses a pid that is not running and (on Windows) an
    image that must never be killed, so a test that wants to observe the kill
    itself has to clear those gates first.
    """
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: alive)
    monkeypatch.setattr(procutil, "windows_process_image", lambda pid: image)


class TestKillProcessTree:
    @pytest.mark.parametrize("pid", [None, 0, -1])
    def test_no_pid_is_a_noop(self, monkeypatch, pid: int | None) -> None:
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        kill_process_tree(pid)  # must not raise, must not run anything

    def test_windows_branch_is_selected_by_os_name(self, monkeypatch) -> None:
        # Runs on every platform: the branch must follow ``os.name``, not the
        # OS the test happens to run on.
        monkeypatch.setattr(os, "name", "nt")
        _stub_kill_gates(monkeypatch)
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> None:
            calls.append(argv)

        monkeypatch.setattr(subprocess, "run", fake_run)
        kill_process_tree(7)
        assert calls == [["taskkill", "/PID", "7", "/T", "/F"]]

    def test_dead_pid_is_never_killed(self, monkeypatch) -> None:
        # The whole point of the gate: a reaped pid is free to be recycled, so
        # naming it in a kill is how a foreign process gets hit instead.
        _stub_kill_gates(monkeypatch, alive=False)
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        monkeypatch.setattr(os, "killpg", _forbid("os.killpg"), raising=False)
        kill_process_tree(4242)  # must do nothing at all

    def test_critical_system_image_is_never_killed(self, monkeypatch) -> None:
        # ``csrss.exe`` and friends cannot die without a bugcheck, so even a
        # live pid that resolves to one must be left alone.
        _stub_kill_gates(monkeypatch, image="csrss.exe")
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        kill_process_tree(4242)

    def test_unidentifiable_windows_image_is_never_killed(self, monkeypatch) -> None:
        # Fails closed: no name means no evidence the pid is still ours.
        _stub_kill_gates(monkeypatch, image=None)
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        kill_process_tree(4242)


@pytest.mark.skipif(not POSIX, reason="process groups are POSIX-only")
class TestKillProcessTreePosix:
    def test_signals_the_process_group(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch)
        seen: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "getpgid", lambda pid: 4242)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: seen.append((pgid, sig)))
        kill_process_tree(1234)
        assert seen == [(4242, signal.SIGKILL)]

    @pytest.mark.parametrize("error", [ProcessLookupError, PermissionError, OSError])
    def test_errors_are_swallowed(self, monkeypatch, error: type[OSError]) -> None:
        _stub_kill_gates(monkeypatch)

        def explode(pgid: int, sig: int) -> None:
            raise error

        monkeypatch.setattr(os, "getpgid", lambda pid: 4242)
        monkeypatch.setattr(os, "killpg", explode)
        kill_process_tree(1234)  # best effort: the caller still has proc.kill()

    def test_missing_group_is_swallowed(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch)

        def missing(pid: int) -> int:
            raise ProcessLookupError

        monkeypatch.setattr(os, "getpgid", missing)
        kill_process_tree(1234)


@pytest.mark.skipif(POSIX, reason="taskkill is the Windows fallback")
class TestKillProcessTreeWindows:
    def test_uses_taskkill_on_the_descendant_tree(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch)
        calls: list[tuple[list[str], dict]] = []

        def fake_run(argv: list[str], **kwargs: object) -> None:
            calls.append((argv, kwargs))

        monkeypatch.setattr(subprocess, "run", fake_run)
        kill_process_tree(4242)
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv == ["taskkill", "/PID", "4242", "/T", "/F"]
        assert kwargs["check"] is False
        assert kwargs["timeout"] == procutil._TASKKILL_TIMEOUT_SECONDS

    @pytest.mark.parametrize(
        "error",
        [FileNotFoundError, subprocess.TimeoutExpired("taskkill", 2)],
    )
    def test_missing_or_hanging_taskkill_is_swallowed(self, monkeypatch, error) -> None:
        _stub_kill_gates(monkeypatch)

        def explode(*args: object, **kwargs: object) -> None:
            raise error

        monkeypatch.setattr(subprocess, "run", explode)
        kill_process_tree(4242)  # best effort: proc.kill() still runs

    def test_dead_pid_does_not_reach_taskkill(self, monkeypatch) -> None:
        # The regression this guards: the suite used to hand ``taskkill`` the
        # pid of a child that had already exited, which on a recycled pid names
        # an unrelated process — and ``/T /F`` takes its whole tree with it.
        _stub_kill_gates(monkeypatch, alive=False)
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        kill_process_tree(4242)

    def test_critical_image_does_not_reach_taskkill(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch, image="lsass.exe")
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        kill_process_tree(4242)

    def test_real_image_of_this_process_is_not_critical(self) -> None:
        # Guards the allow-list itself: whatever runs the suite (python.exe, or
        # something else) must not be mistaken for a critical system image.
        image = windows_process_image(os.getpid())
        assert image not in procutil._CRITICAL_PROCESS_IMAGES

    def test_real_image_of_this_process_is_readable(self) -> None:
        assert windows_process_image(os.getpid()) is not None

    def test_unknown_pid_image_is_none(self) -> None:
        # An image can only be read through a handle, so a pid that is gone
        # fails closed rather than answering with a guess.
        assert windows_process_image(0) is None

    def test_pid_kill_refusal_reports_a_dead_pid(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch, alive=False)
        refusal = procutil._kill_by_pid_refusal(4242)
        assert refusal is not None and "not running" in refusal

    def test_pid_kill_refusal_reports_a_critical_image(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch, image="csrss.exe")
        refusal = procutil._kill_by_pid_refusal(4242)
        assert refusal is not None and "critical" in refusal

    def test_pid_kill_refusal_allows_a_live_ordinary_process(self, monkeypatch) -> None:
        _stub_kill_gates(monkeypatch, image="cmd.exe")
        assert procutil._kill_by_pid_refusal(4242) is None


# ── process_group_of / process_group_alive ───────────────────────────────────
# The real POSIX symbols cannot be stubbed *and* used on Windows: ``os.killpg``
# only exists on POSIX, and so does ``signal.SIGKILL``. The cases that must run
# the POSIX branch are therefore skipped there, while the Windows/symbol-missing
# behaviour is exercised everywhere by patching ``os.name``.


class TestProcessGroupOf:
    @pytest.mark.parametrize("pid", [None, 0, -1])
    def test_no_pid_has_no_group(self, monkeypatch, pid: int | None) -> None:
        monkeypatch.setattr(os, "getpgid", _forbid("os.getpgid"), raising=False)
        assert process_group_of(pid) is None

    def test_windows_has_no_process_groups(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(os, "getpgid", _forbid("os.getpgid"), raising=False)
        assert process_group_of(os.getpid()) is None

    @pytest.mark.skipif(
        not POSIX, reason="needs the real os.getpgid, absent on Windows"
    )
    def test_posix_asks_the_kernel(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "getpgid", lambda pid: 4242)
        assert process_group_of(1234) == 4242

    @pytest.mark.skipif(
        not POSIX, reason="needs the real os.getpgid, absent on Windows"
    )
    @pytest.mark.parametrize("error", [ProcessLookupError, PermissionError, OSError])
    def test_failed_query_reports_no_group(
        self, monkeypatch, error: type[Exception]
    ) -> None:
        monkeypatch.setattr(os, "getpgid", _raiser(error))
        assert process_group_of(1234) is None


class TestProcessGroupAlive:
    @pytest.mark.parametrize("pgid", [None, 0, -1])
    def test_falsey_or_negative_group_is_not_alive(self, monkeypatch, pgid) -> None:
        monkeypatch.setattr(os, "killpg", _forbid("os.killpg"), raising=False)
        assert process_group_alive(pgid) is False

    def test_windows_degrades_to_the_pid_probe(self, monkeypatch) -> None:
        # Windows has no groups, so the caller tracks the child pid instead.
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(procutil, "pid_alive_windows", lambda pid: pid == 4242)
        assert process_group_alive(4242) is True
        assert process_group_alive(4243) is False

    @pytest.mark.skipif(not POSIX, reason="os.killpg is POSIX-only")
    def test_probes_with_signal_zero(self, monkeypatch) -> None:
        seen: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: seen.append((pgid, sig)))
        assert process_group_alive(4242) is True
        assert seen == [(4242, 0)]  # signal 0 is a probe: nothing is delivered

    @pytest.mark.skipif(not POSIX, reason="os.killpg is POSIX-only")
    def test_missing_group_is_gone(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "killpg", _raiser(ProcessLookupError))
        assert process_group_alive(4242) is False

    @pytest.mark.skipif(not POSIX, reason="os.killpg is POSIX-only")
    def test_permission_error_means_alive(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "killpg", _raiser(PermissionError))
        assert process_group_alive(4242) is True

    @pytest.mark.skipif(not POSIX, reason="os.killpg is POSIX-only")
    def test_other_os_error_means_gone(self, monkeypatch) -> None:
        monkeypatch.setattr(os, "killpg", _raiser(OSError))
        assert process_group_alive(4242) is False

    @pytest.mark.skipif(POSIX, reason="os.killpg exists here")
    def test_absent_killpg_falls_back_to_the_pid_probe(self, monkeypatch) -> None:
        # The Windows situation: the symbol simply is not there. Probing must
        # degrade to the pid probe (OpenProcess) instead of raising
        # ``AttributeError`` out of the startup reconciler.
        monkeypatch.setattr(procutil, "pid_alive_windows", lambda pid: True)
        assert process_group_alive(123) is True


# ── kill_process_group ───────────────────────────────────────────────────────


class TestKillProcessGroup:
    @pytest.mark.parametrize("pgid", [None, 0, -1])
    def test_no_group_is_a_noop(self, monkeypatch, pgid: int | None) -> None:
        monkeypatch.setattr(os, "killpg", _forbid("os.killpg"), raising=False)
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        kill_process_group(pgid)  # must not raise, must not signal anything

    def test_windows_falls_back_to_the_tree_kill(self, monkeypatch) -> None:
        # The tracked "group" on Windows is the child pid, so the fallback has
        # to walk the descendant tree (``taskkill /T``). ``taskkill`` exists on
        # no platform under test, so ``subprocess.run`` is what is stubbed.
        calls: list[list[str]] = []
        monkeypatch.setattr(os, "name", "nt")
        _stub_kill_gates(monkeypatch)
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))
        kill_process_group(4242)
        assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]

    def test_empty_group_is_never_signalled(self, monkeypatch) -> None:
        monkeypatch.setattr(procutil, "process_group_alive", lambda pgid: False)
        monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
        monkeypatch.setattr(os, "killpg", _forbid("os.killpg"), raising=False)
        kill_process_group(4242)


@pytest.mark.skipif(not POSIX, reason="process groups are POSIX-only")
class TestKillProcessGroupPosix:
    def test_kills_the_group_directly(self, monkeypatch) -> None:
        # Signalled by group id, not resolved from a pid: the leader may
        # already be gone while its grandchildren are still in the group.
        monkeypatch.setattr(procutil, "process_group_alive", lambda pgid: True)
        seen: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "getpgid", _forbid("os.getpgid"))
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: seen.append((pgid, sig)))
        kill_process_group(4242)
        assert seen == [(4242, signal.SIGKILL)]

    @pytest.mark.parametrize("error", [ProcessLookupError, PermissionError, OSError])
    def test_errors_are_swallowed(self, monkeypatch, error: type[Exception]) -> None:
        monkeypatch.setattr(procutil, "process_group_alive", lambda pgid: True)
        monkeypatch.setattr(os, "killpg", _raiser(error))
        kill_process_group(4242)  # best effort: proc.kill() still runs


# ── public surface ───────────────────────────────────────────────────────────


def test_public_api_is_complete() -> None:
    assert set(procutil.__all__) == {
        "pid_alive",
        "SessionKwargs",
        "pid_alive_windows",
        "kill_process_tree",
        "session_kwargs",
        "process_group_of",
        "kill_process_group",
        "process_group_alive",
        "windows_process_image",
    }


def test_every_exported_name_exists() -> None:
    for name in procutil.__all__:
        assert hasattr(procutil, name), name
