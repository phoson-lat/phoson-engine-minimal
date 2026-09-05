"""Algorithmic task: merge two sorted lists into one sorted list.

The expected output is a single deterministic sorted sequence; the
checker verifies exact order (so a stable-but-unsorted or unstable
result is detectable, not just a bag of numbers).
"""

from pathlib import Path

NAME = "merge-sorted-lists"

A = [1, 4, 7, 9, 12, 15]
B = [2, 4, 6, 8, 12, 20]
# Expected stable merge of A then B:
EXPECTED = [1, 2, 4, 4, 6, 7, 8, 9, 12, 12, 15, 20]


def setup(workspace: Path) -> None:
    (workspace / "a.txt").write_text("\n".join(str(x) for x in A) + "\n")
    (workspace / "b.txt").write_text("\n".join(str(x) for x in B) + "\n")


INSTRUCTION = (
    "The files a.txt and b.txt each contain a sorted list of integers, "
    "one per line. Create a file called merged.txt containing the merged "
    "list of both, sorted in non-decreasing order, one integer per line, "
    "preserving every value (duplicates included)."
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "merged.txt"
    if not path.exists():
        return False, "merged.txt not created"
    got = [int(x) for x in path.read_text().split() if x.lstrip("-").isdigit()]
    if got != EXPECTED:
        return False, f"expected {EXPECTED}, got {got}"
    return True, "merge is correctly sorted and complete"


def SOLVE(workspace: Path) -> None:
    a = [int(x) for x in (workspace / "a.txt").read_text().split()]
    b = [int(x) for x in (workspace / "b.txt").read_text().split()]
    (workspace / "merged.txt").write_text("\n".join(map(str, sorted(a + b))) + "\n")
