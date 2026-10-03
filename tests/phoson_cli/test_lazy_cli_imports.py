"""Deterministic regression guards for the CLI entrypoint's lazy imports.

``phoson_cli.__main__`` must stay importable without dragging in the heavy
interactive stack (``prompt_toolkit``/``rich``/``pygments``) or the deferred
subsystems (``phoson_cli.repl``, ``phoson_cli.fullscreen.app``,
``phoson_cli.installer``, ``phoson_cli.updater``). Those are only needed when
an interactive front end, the setup wizard or an update actually runs.

Everything here runs in a *clean subprocess*: the pytest process has already
imported the whole stack, so an in-process ``sys.modules`` check would be a
false positive. The assertions are module-presence checks — no timing — so
they cannot flake.
"""

import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Heavy UI / deferred modules a plain entrypoint import must not load.
HEAVY_UI = ("prompt_toolkit", "rich", "pygments")
DEFERRED = (
    "phoson_cli.repl",
    "phoson_cli.fullscreen.app",
    "phoson_cli.installer",
    "phoson_cli.updater",
)


def _run_probe(body: str) -> subprocess.CompletedProcess[str]:
    """Run *body* in a fresh interpreter and report what it loaded.

    The probe always ends by printing ``HEAVY=<comma list>`` and
    ``DEFERRED=<comma list>`` so the caller can assert on module presence.
    """
    code = (
        "import sys\n"
        f"{body}\n"
        "print('HEAVY=' + ','.join(m for m in "
        f"{HEAVY_UI!r} if m in sys.modules))\n"
        "print('DEFERRED=' + ','.join(m for m in "
        f"{DEFERRED!r} if m in sys.modules))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def _parse_marker(stdout: str, marker: str) -> set[str]:
    for line in stdout.splitlines():
        if line.startswith(f"{marker}="):
            return {m for m in line[len(marker) + 1 :].split(",") if m}
    raise AssertionError(f"probe did not emit {marker}=: {stdout!r}")


# ── plain import stays light ─────────────────────────────────────────────────


def test_import_entrypoint_loads_no_heavy_ui_stack() -> None:
    result = _run_probe("import phoson_cli.__main__")
    assert result.returncode == 0, result.stderr
    assert _parse_marker(result.stdout, "HEAVY") == set()


def test_import_entrypoint_defers_frontends_and_subsystems() -> None:
    result = _run_probe("import phoson_cli.__main__")
    assert result.returncode == 0, result.stderr
    assert _parse_marker(result.stdout, "DEFERRED") == set()


def test_import_config_stays_lightweight() -> None:
    result = _run_probe("import phoson_cli.config")
    assert result.returncode == 0, result.stderr
    assert _parse_marker(result.stdout, "HEAVY") == set()


# ── help / version fast paths run without the heavy stack ────────────────────


def _run_main_probe(arg: str) -> subprocess.CompletedProcess[str]:
    body = (
        "import io\n"
        "import sys\n"
        "import contextlib\n"
        "import phoson_cli.__main__ as m\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        f"    sys.argv = ['phoson-cli', {arg!r}]\n"
        "    try:\n"
        "        m.main()\n"
        "    except SystemExit as exc:\n"
        "        code = 0 if exc.code is None else exc.code\n"
        "    else:\n"
        "        code = 0\n"
        "print('EXIT=' + str(code))\n"
        "print('OUT=' + buf.getvalue().strip())\n"
    )
    return _run_probe(body)


def test_help_path_is_lightweight_and_exits_zero() -> None:
    result = _run_main_probe("--help")
    assert result.returncode == 0, result.stderr
    assert "EXIT=0" in result.stdout
    assert _parse_marker(result.stdout, "HEAVY") == set()
    assert _parse_marker(result.stdout, "DEFERRED") == set()
    # The usage text (multi-line) is part of the captured stdout.
    assert "--version" in result.stdout
    assert "--classic" in result.stdout


def test_version_path_is_lightweight_and_exits_zero() -> None:
    result = _run_main_probe("--version")
    assert result.returncode == 0, result.stderr
    assert "EXIT=0" in result.stdout
    assert _parse_marker(result.stdout, "HEAVY") == set()
    assert _parse_marker(result.stdout, "DEFERRED") == set()
    out = next(line for line in result.stdout.splitlines() if line.startswith("OUT="))
    assert out.startswith("OUT=phoson-cli ")


# ── process-level determinism for the real module invocation ─────────────────


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "phoson_cli", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_module_help_process() -> None:
    result = _run_module("--help")
    assert result.returncode == 0, result.stderr
    assert "phoson-cli [options] [task]" in result.stdout
    assert "--version" in result.stdout


def test_module_version_process() -> None:
    result = _run_module("--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("phoson-cli ")


# ── lazy attribute access still resolves the real classes ────────────────────


def test_lazy_class_attributes_resolve_on_access() -> None:
    body = (
        "import phoson_cli.__main__ as m\n"
        "assert m.PhosonRepl.__name__ == 'PhosonRepl'\n"
        "assert m.PhosonApp.__name__ == 'PhosonApp'\n"
    )
    result = _run_probe(body)
    assert result.returncode == 0, result.stderr


def test_lightweight_version_matches_the_updater() -> None:
    """Control: the import-light ``--version`` helper must not drift from the
    canonical implementation in :mod:`phoson_cli.updater`."""
    body = (
        "import phoson_cli.__main__ as m\n"
        "from phoson_cli.updater import get_current_version as canonical\n"
        "assert m.get_current_version() == canonical(), "
        "(m.get_current_version(), canonical())\n"
    )
    result = _run_probe(body)
    assert result.returncode == 0, result.stderr
