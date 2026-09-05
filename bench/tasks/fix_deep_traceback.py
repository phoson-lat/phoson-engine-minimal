"""Root-cause task: the wrong output is a red herring.

Running the entrypoint produces a wrong number (each Row is worth 2× its
value), and plausible band-aids — hard-coding the answer, or massaging
main.py/pipeline.py — are detectable and rejected. The real bug is one
level deep in the import chain (models.py).
"""

from pathlib import Path

NAME = "fix-deep-traceback"

SRC = {
    "main.py": (
        "from pipeline import extract\n\n"
        "if __name__ == '__main__':\n"
        "    print(sum(extract()))\n"
    ),
    "pipeline.py": (
        "from models import Row\n\n"
        "def extract():\n"
        "    rows = [Row(5), Row(7)]\n"
        "    return [r.value() for r in rows]\n"
    ),
    "models.py": (
        "class Row:\n"
        "    def __init__(self, v):\n"
        "        self._v = v\n"
        "    def value(self):\n"
        "        return self._v * 2\n"  # BUG: should be self._v
    ),
}


def setup(workspace: Path) -> None:
    for rel, content in SRC.items():
        (workspace / rel).write_text(content)


INSTRUCTION = (
    "Running `python main.py` in this project prints a wrong number. "
    "Each Row's value() should return the value it was constructed with, "
    "so the program should print the sum of the two rows, 5 + 7 = 12. "
    "Fix the underlying bug so `python main.py` prints exactly 12 and "
    "exits 0. Do not change the expected total or hard-code the output."
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    import subprocess

    proc = subprocess.run(
        ["python3", "main.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return False, f"main.py still fails: {proc.stderr[:200]}"
    if proc.stdout.strip() != "12":
        return False, f"expected '12', got {proc.stdout.strip()!r}"
    # Reject band-aids: main.py must still compute sum(extract()), not a
    # hard-coded 12, and the *2 bug must actually be gone from models.py.
    if "print(12)" in (workspace / "main.py").read_text():
        return False, "main.py hard-codes the answer (not a real fix)"
    if "self._v * 2" in (workspace / "models.py").read_text():
        return False, "the real bug in models.py.value() is still present"
    return True, "root-cause fixed, prints 12"


def SOLVE(workspace: Path) -> None:
    """Fix the real bug: value() must return the stored value, not 2× it."""
    (workspace / "models.py").write_text(
        "class Row:\n"
        "    def __init__(self, v):\n"
        "        self._v = v\n"
        "    def value(self):\n"
        "        return self._v\n"
    )
