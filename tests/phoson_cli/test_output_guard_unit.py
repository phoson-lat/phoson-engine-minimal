"""Tests for the stray-output guard (issue #148).

The guard redirects ``sys.stderr`` to a log file + in-memory tail while a
front end runs so stray prints/logging cannot tear the render. ``sys.stdout``
must stay untouched (it is the prompt_toolkit paint channel and the one-shot
result channel).
"""

import sys

import pytest

from phoson_cli.output_guard import OutputGuard, current_guard


def test_stderr_is_captured_into_tail(tmp_path):
    log = tmp_path / "stderr.log"
    g = OutputGuard(log_path=log)
    g.install()
    try:
        assert current_guard() is g
        print("captured-line-XYZ", file=sys.stderr)
        assert any("captured-line-XYZ" in ln for ln in g.last_lines())
    finally:
        g.restore()
    assert current_guard() is None


def test_stdout_is_not_redirected(tmp_path):
    real_stdout = sys.stdout
    g = OutputGuard(log_path=tmp_path / "x.log")
    g.install()
    try:
        # The paint / result channel must be left exactly as we found it.
        assert sys.stdout is real_stdout
    finally:
        g.restore()


def test_stderr_is_restored_and_restore_is_idempotent(tmp_path):
    real = sys.stderr
    g = OutputGuard(log_path=tmp_path / "y.log")
    g.install()
    assert sys.stderr is not real
    g.restore()
    assert sys.stderr is real
    g.restore()  # second restore is a no-op
    assert sys.stderr is real


def test_write_lands_in_log_file(tmp_path):
    log = tmp_path / "z.log"
    g = OutputGuard(log_path=log)
    g.install()
    try:
        print("file-line-ABC", file=sys.stderr)
    finally:
        g.restore()
    assert log.exists()
    assert "file-line-ABC" in log.read_text(encoding="utf-8")


def test_context_manager_installs_and_restores(tmp_path):
    log = tmp_path / "ctx.log"
    with OutputGuard(log_path=log) as g:
        assert current_guard() is g
        print("ctx-line-DEF", file=sys.stderr)
    assert current_guard() is None
    assert "ctx-line-DEF" in log.read_text(encoding="utf-8")


def test_degrades_to_memory_when_path_unwritable(tmp_path):
    # Make the parent a regular file so mkdir(parents=True) fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    bad = blocker / "sub" / "x.log"

    g = OutputGuard(log_path=bad)
    g.install()
    try:
        print("still-captured-GHI", file=sys.stderr)  # must not raise
        assert any("still-captured-GHI" in ln for ln in g.last_lines())
    finally:
        g.restore()


def test_last_lines_bounded(tmp_path):
    g = OutputGuard(log_path=tmp_path / "b.log")
    g.install()
    try:
        for i in range(10):
            print(f"line-{i}", file=sys.stderr)
        tail = g.last_lines(5)
        assert len(tail) == 5
        assert tail[-1].endswith("line-9")
    finally:
        g.restore()


@pytest.fixture(autouse=True)
def _no_cross_test_leak():
    """Ensure no active guard leaks between tests."""
    yield
    from phoson_cli.output_guard import _CURRENT

    _CURRENT[0] = None
