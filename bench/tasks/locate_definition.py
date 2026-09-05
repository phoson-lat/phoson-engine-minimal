"""Search task: locate a symbol's definition in a moderately sized tree.

The agent must search both for the definition and for the callers, then
report where it lives. The tree is big enough that ``list_dir`` alone
(depth 3) does not surface the answer cheaply.
"""

from pathlib import Path

NAME = "locate-definition"
INSTRUCTION = (
    "The function `compute_totals` is defined somewhere in this project, "
    "and `api/routes.py` calls it. Create a file named answer.txt "
    "containing ONLY the relative path of the file where `compute_totals` "
    "is DEFINED (not where it is called)."
)


def setup(workspace: Path) -> None:
    # 3-level-deep definition with siblings that look plausible.
    (workspace / "api").mkdir()
    (workspace / "api" / "routes.py").write_text(
        "from services.reports.totals import compute_totals\n\n"
        "def endpoint():\n    return compute_totals()\n",
        encoding="utf-8",
    )
    (workspace / "services" / "reports").mkdir(parents=True)
    (workspace / "services" / "reports" / "__init__.py").write_text("")
    (workspace / "services" / "reports" / "totals.py").write_text(
        "def compute_totals():\n    return {'a': 1, 'b': 2}\n",
        encoding="utf-8",
    )

    # Decoys: same name as a variable/attribute, similar names elsewhere.
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_totals.py").write_text(
        "def test_it():\n    totals = compute_totals()\n    assert totals\n",
        encoding="utf-8",
    )
    (workspace / "docs").mkdir()
    (workspace / "docs" / "notes.md").write_text(
        "compute_totals was split from metrics.py in 2026.\n", encoding="utf-8"
    )
    (workspace / "services" / "reports" / "metrics.py").write_text(
        "def compute_metrics():\n    return {}\n", encoding="utf-8"
    )

    # Padding to push the tree past list_dir's shallow view.
    for i in range(6):
        (workspace / "vendor" / f"pkg{i}").mkdir(parents=True)
        (workspace / "vendor" / f"pkg{i}" / "mod.py").write_text(
            f"# vendor module {i}\nVALUE = {i}\n", encoding="utf-8"
        )


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "answer.txt"
    if not path.exists():
        return False, "answer.txt not created"
    got = path.read_text().strip()
    if got != "services/reports/totals.py":
        return False, f"expected 'services/reports/totals.py', got {got!r}"
    return True, "definition located"


def SOLVE(workspace: Path) -> None:
    (workspace / "answer.txt").write_text("services/reports/totals.py\n")
