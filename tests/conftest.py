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
"""

import os
import atexit
import shutil
import tempfile
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
