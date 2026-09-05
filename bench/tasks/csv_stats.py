"""Data-analysis task: compute stats from a CSV using bash/python."""

from pathlib import Path

NAME = "csv-stats"
INSTRUCTION = (
    "The file data.csv contains rows 'name,score'. Create a file called "
    "result.txt containing only the average of the score column rounded "
    "to 2 decimal places (e.g. 12.34)."
)


def setup(workspace: Path) -> None:
    scores = [10, 20, 30, 40, 55]  # mean = 31.0
    lines = ["name,score"] + [f"user{i},{s}" for i, s in enumerate(scores)]
    (workspace / "data.csv").write_text("\n".join(lines) + "\n")


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "result.txt"
    if not path.exists():
        return False, "result.txt not created"
    value = path.read_text().strip()
    if value != "31.00":
        return False, f"expected '31.00', got {value!r}"
    return True, "average computed correctly"


def SOLVE(workspace: Path) -> None:
    (workspace / "result.txt").write_text("31.00\n")
