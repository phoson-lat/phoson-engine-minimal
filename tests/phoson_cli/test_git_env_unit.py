"""Regression tests for the system prompt's git ``# Environment`` block.

Two guarantees, exactly the ones that were failing on Windows:

1. ``_git_output`` is **bounded**: a hung git (or one that leaves a child alive
   holding the pipe) must return ``None`` promptly, never block the caller -
   which is the event loop of every front end.
2. ``_git_env_block`` is **cached**: ``build_system_prompt`` runs on every turn
   *and* on every context-meter refresh, and two git spawns per call are pure
   overhead.

Background on the original bug (CPython ``subprocess.run``): when ``timeout=``
expires it kills only the direct child and then calls ``communicate()``
**without a timeout** to collect the partial output. On Windows ``git``/``python``
are shim chains: the grandchild survives with the pipe open and that
``communicate()`` blocks *forever*.
"""

import sys
import time
from pathlib import Path

import phoson_cli.session_utils as su
from phoson_cli.session_utils import build_system_prompt


def _tool(name: str):
    from types import SimpleNamespace

    return SimpleNamespace(name=name)


# ── 1. the hang ──────────────────────────────────────────────────────────────


def test_git_output_is_bounded_when_the_command_hangs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    out = su._git_output(["-c", "import time; time.sleep(60)"], tmp_path)
    elapsed = time.monotonic() - started

    assert out is None
    assert elapsed < 5, f"_git_output blocked its caller for {elapsed:.1f}s"


def test_git_output_survives_a_child_that_forks_and_keeps_the_pipe(
    monkeypatch, tmp_path
) -> None:
    """The exact Windows case: a 'shim' that leaves a child alive inheriting
    stdout. The call must still return, bounded."""
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 0.2)

    grandchild = "'-c','import time; time.sleep(60)'"
    script = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,{grandchild}]);"
        "time.sleep(60)"
    )
    started = time.monotonic()
    out = su._git_output(["-c", script], tmp_path)
    elapsed = time.monotonic() - started

    assert out is None
    assert elapsed < 5, f"_git_output blocked its caller for {elapsed:.1f}s"


def test_git_output_returns_stdout_on_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 10)
    assert su._git_output(["-c", "print('main')"], tmp_path) == "main\n"


def test_git_output_is_none_when_the_command_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 10)
    assert su._git_output(["-c", "raise SystemExit(3)"], tmp_path) is None


def test_git_output_never_reads_stdin(monkeypatch, tmp_path) -> None:
    """git must not inherit the front end's protocol pipe (it could steal
    NDJSON framing bytes)."""
    import subprocess

    seen: dict[str, object] = {}
    real_popen = subprocess.Popen

    class _Spy(real_popen):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            seen["stdin"] = kwargs.get("stdin")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(subprocess, "Popen", _Spy)
    assert su._git_output(["-c", "print('ok')"], tmp_path) == "ok\n"
    assert seen["stdin"] is subprocess.DEVNULL


# ── 2. the cache ─────────────────────────────────────────────────────────────


def test_git_env_block_is_cached_per_cwd(monkeypatch, tmp_path) -> None:
    calls: list[tuple[tuple, Path]] = []

    def _counting(args, cwd):
        calls.append((tuple(args), Path(cwd)))
        return "main\n"

    monkeypatch.setattr(su, "_git_output", _counting)
    su._git_env_cache.pop(str(tmp_path), None)

    first = su._git_env_block(tmp_path)
    second = su._git_env_block(tmp_path)

    assert first == second
    assert "# Environment" in first
    # branch + status, once: the second call is served from the cache.
    assert len(calls) == 2


def test_git_env_block_force_bypasses_the_cache(monkeypatch, tmp_path) -> None:
    calls: list[int] = []

    def _counting(args, cwd):
        calls.append(1)
        return "main\n"

    monkeypatch.setattr(su, "_git_output", _counting)
    su._git_env_cache.pop(str(tmp_path), None)

    su._git_env_block(tmp_path)
    su._git_env_block(tmp_path, force=True)
    assert len(calls) == 4


def test_git_env_block_caches_a_broken_git_as_absent(monkeypatch, tmp_path) -> None:
    """A hung/missing git degrades to *no* block and is not retried on every
    turn within the TTL."""
    calls: list[int] = []

    def _broken(args, cwd):
        calls.append(1)
        return None

    monkeypatch.setattr(su, "_git_output", _broken)
    su._git_env_cache.pop(str(tmp_path), None)

    assert su._git_env_block(tmp_path) == ""
    assert su._git_env_block(tmp_path) == ""
    assert len(calls) == 1


# ── 3. the block can be resolved off the event loop ──────────────────────────


def test_build_system_prompt_accepts_a_pre_resolved_env_block(monkeypatch) -> None:
    """Async front ends resolve git with ``asyncio.to_thread`` and pass the
    block in: the builder must not shell out at all."""

    def _boom(args, cwd):
        raise AssertionError("git must not be called when env_block is passed")

    monkeypatch.setattr(su, "_git_output", _boom)
    prompt = build_system_prompt(
        [_tool("bash")], env_block="\n\n# Environment\n- git branch: test"
    )
    assert "- git branch: test" in prompt


def test_build_system_prompt_still_resolves_env_block_by_default(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(su, "_git_output", lambda args, cwd: None)
    # The cache is global per cwd: clear it so the test is deterministic
    # wherever it runs in the suite.
    monkeypatch.setattr(su, "_git_env_cache", {})
    assert "# Environment" not in build_system_prompt([_tool("bash")])
