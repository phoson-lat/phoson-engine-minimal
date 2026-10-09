"""Regresiones del bloque ``# Environment`` (git) del system prompt.

Dos garantías, exactamente las que fallaban en Windows:

1. ``_git_output`` está **acotado**: un git que se cuelga (o que deja un
   descendiente vivo sujetando el pipe) debe devolver ``None`` rápido, nunca
   bloquear al llamador — que es el event loop de cualquier front end.
2. ``_git_env_block`` está **cacheado**: ``build_system_prompt`` se ejecuta en
   cada turno *y* en cada refresco del medidor de contexto, y dos spawns de
   git por llamada son puro overhead.

Detalle del bug original (CPython ``subprocess.run``): al vencer ``timeout=``
mata solo al hijo directo y después llama a ``communicate()`` **sin timeout**
para recoger la salida parcial. En Windows ``git``/``python`` son cadenas de
shims: el nieto sobrevive con el pipe abierto y esa ``communicate()`` se
queda bloqueada *para siempre*.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

import phoson_cli.session_utils as su
from phoson_cli.session_utils import build_system_prompt


def _tool(name: str):
    from types import SimpleNamespace

    return SimpleNamespace(name=name)


# ── 1. el bloqueo ────────────────────────────────────────────────────────────


def test_git_output_is_bounded_when_the_command_hangs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    out = su._git_output(["-c", "import time; time.sleep(60)"], tmp_path)
    elapsed = time.monotonic() - started

    assert out is None
    assert elapsed < 5, f"_git_output bloqueó al llamador {elapsed:.1f}s"


def test_git_output_survives_a_child_that_forks_and_keeps_the_pipe(
    monkeypatch, tmp_path
) -> None:
    """El caso exacto de Windows: un 'shim' que deja un descendiente vivo
    heredando stdout. La llamada debe volver igual, acotada."""
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
    assert elapsed < 5, f"_git_output bloqueó al llamador {elapsed:.1f}s"


def test_git_output_returns_stdout_on_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 10)
    assert su._git_output(["-c", "print('main')"], tmp_path) == "main\n"


def test_git_output_is_none_when_the_command_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_GIT_BIN", sys.executable)
    monkeypatch.setattr(su, "_GIT_TIMEOUT_SECONDS", 10)
    assert su._git_output(["-c", "raise SystemExit(3)"], tmp_path) is None


def test_git_output_never_reads_stdin(monkeypatch, tmp_path) -> None:
    """git no debe heredar el pipe de protocolo del front end (podría robar
    bytes del framing NDJSON)."""
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


# ── 2. la caché ──────────────────────────────────────────────────────────────


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
    # branch + status, una sola vez: la segunda llamada sale de la caché.
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
    """Un git colgado/ausente degrada a *sin* bloque y no se vuelve a intentar
    en cada turno dentro del TTL."""
    calls: list[int] = []

    def _broken(args, cwd):
        calls.append(1)
        return None

    monkeypatch.setattr(su, "_git_output", _broken)
    su._git_env_cache.pop(str(tmp_path), None)

    assert su._git_env_block(tmp_path) == ""
    assert su._git_env_block(tmp_path) == ""
    assert len(calls) == 1


# ── 3. el bloque se puede resolver fuera del event loop ──────────────────────


def test_build_system_prompt_accepts_a_pre_resolved_env_block(monkeypatch) -> None:
    """Los front ends asyncio resuelven git con ``asyncio.to_thread`` y pasan
    el bloque: el builder no debe llamar a git en absoluto."""

    def _boom(args, cwd):
        raise AssertionError("git no debe invocarse cuando se pasa env_block")

    monkeypatch.setattr(su, "_git_output", _boom)
    prompt = build_system_prompt([_tool("bash")], env_block="\n\n# Environment\n- git branch: test")
    assert "- git branch: test" in prompt


def test_build_system_prompt_still_resolves_env_block_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(su, "_git_output", lambda args, cwd: None)
    # La caché es global por cwd: se limpia para que el test sea determinista
    # pase en el orden que pase dentro de la suite.
    monkeypatch.setattr(su, "_git_env_cache", {})
    assert "# Environment" not in build_system_prompt([_tool("bash")])
