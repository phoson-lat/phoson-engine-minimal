"""Search+aggregate task: count total function definitions across a tree.

The answer requires walking a deep, padded tree and counting `def `
declarations in every .py file — cheap to script, hard to do by eye,
and the checker recomputes the ground truth from the generated tree.
"""

from pathlib import Path

NAME = "count-defs-in-tree"

# (path, number of defs) — the generator writes exactly this many.
PLAN = [
    ("src/core/a.py", 3),
    ("src/core/b.py", 2),
    ("src/io/read.py", 4),
    ("src/io/write.py", 1),
    ("tools/lint.py", 2),
]


def setup(workspace: Path) -> None:
    for rel, n in PLAN:
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(n))
        p.write_text(body)
    # Padding that must NOT be counted: .txt files with "def " in prose,
    # and a .py file that only *calls* functions.
    (workspace / "notes").mkdir()
    (workspace / "notes" / "ideas.txt").write_text(
        "def a better naming later\nTODO: def the API\n"
    )
    (workspace / "src" / "main.py").write_text("from core.a import f0\n\nprint(f0())\n")


INSTRUCTION = (
    "Count how many function definitions (lines starting with 'def ') "
    "there are in all .py files in this project. Create a file called "
    "answer.txt containing ONLY that total, as a bare integer."
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    expected = sum(n for _, n in PLAN)
    # Recompute ground truth from the workspace (not just the plan) so a
    # task bug can't silently invalidate the check.
    computed = 0
    for p in (workspace).rglob("*.py"):
        computed += sum(1 for ln in p.read_text().splitlines() if ln.startswith("def "))
    assert computed == expected, f"task bug: computed {computed} != planned {expected}"
    path = workspace / "answer.txt"
    if not path.exists():
        return False, "answer.txt not created"
    value = path.read_text().strip()
    if value != str(expected):
        return False, f"expected {expected}, got {value!r}"
    return True, f"count matches ({expected})"


def SOLVE(workspace: Path) -> None:
    c = 0
    for p in workspace.rglob("*.py"):
        c += sum(1 for ln in p.read_text().splitlines() if ln.startswith("def "))
    (workspace / "answer.txt").write_text(f"{c}\n")
