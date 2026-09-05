"""Fix a failing Python script: it must exit 0 and print the right sum."""

import subprocess
from pathlib import Path

NAME = "fix-failing-script"
INSTRUCTION = (
    "The file calc.py in the current directory crashes when run. "
    "Fix it so that running `python calc.py` prints exactly 42 and exits 0. "
    "Do not rewrite the file from scratch; make a minimal fix."
)


def setup(workspace: Path) -> None:
    (workspace / "calc.py").write_text(
        "def add(a, b):\n    return a - b  # BUG: should be +\n\n"
        "if __name__ == '__main__':\n    print(add(40, 2))\n"
    )


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    proc = subprocess.run(
        ["python3", "calc.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return False, f"calc.py still fails: {proc.stderr[:200]}"
    if proc.stdout.strip() != "42":
        return False, f"expected '42', got {proc.stdout.strip()!r}"
    return True, "calc.py prints 42"


def SOLVE(workspace: Path) -> None:
    """The minimal fix: the operator on line 2 must be ``+``, not ``-``."""
    (workspace / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "if __name__ == '__main__':\n    print(add(40, 2))\n"
    )
