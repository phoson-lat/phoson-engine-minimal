"""Run the bridge's script harnesses and the JSON-RPC smoke test.

The harnesses in ``harness/`` assert at import time and patch module globals, so
each one runs in its own process with an isolated ``HOME`` (they were written
that way in Phoson Desktop, where the bridge lived before). The smoke test
starts ``python -m phoson_bridge`` and drives the control plane over stdio,
without calling an LLM.
"""

import os
import sys
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HARNESSES = sorted(
    p for p in (HERE / "harness").glob("*.py") if p.name != "__init__.py"
)


def _env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PHOSON_HOME": str(home / ".phoson"),
        # A provider that needs no API key, so engines can be built offline.
        "PHOSON_PROVIDER": "ollama",
        "PHOSON_MODEL": "test-model",
        "PYTHONIOENCODING": "utf-8",
    }


@pytest.mark.parametrize("script", HARNESSES, ids=lambda p: p.stem)
def test_harness(script: Path, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


def test_json_rpc_smoke(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bridge_smoke.py")],
        cwd=ROOT,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "TODO OK" in result.stdout
