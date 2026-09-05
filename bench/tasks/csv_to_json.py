"""Transformation task: CSV → JSON with correct types, all rows.

Discriminates real multi-row transformation (not just the first line)
and type coercion (string→number/bool) from a lazy copy.
"""

import json
from pathlib import Path

NAME = "csv-to-json"
ROWS = [
    ("alice", 82, "true"),
    ("bob", 91, "false"),
    ("carol", 74, "true"),
    ("dave", 63, "false"),
    ("erin", 99, "true"),
]


def setup(workspace: Path) -> None:
    lines = ["name,score,active"] + [f"{n},{s},{a}" for n, s, a in ROWS]
    (workspace / "data.csv").write_text("\n".join(lines) + "\n")


INSTRUCTION = (
    "The file data.csv has columns name,score,active (5 data rows). "
    "Create a file called people.json containing a JSON array with one "
    "object per row, in the same order, where: name is a string, score "
    "is a number (not a string), and active is a boolean (not a string)."
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "people.json"
    if not path.exists():
        return False, "people.json not created"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    expected = [{"name": n, "score": int(s), "active": a == "true"} for n, s, a in ROWS]
    if data != expected:
        return False, f"content mismatch: {data}"
    return True, "people.json matches all rows with correct types"


def SOLVE(workspace: Path) -> None:
    (workspace / "people.json").write_text(
        json.dumps(
            [{"name": n, "score": int(s), "active": a == "true"} for n, s, a in ROWS]
        )
    )
