"""Counting task: exact occurrence count over a large generated log.

Discriminates real counting at scale from guessing or sampling: the log
is big enough that eyeballing is not feasible, but the answer is
deterministic and the checker recomputes it from the file.
"""

from pathlib import Path

NAME = "log-error-count"
INSTRUCTION = (
    "The file logs/app.log has many lines. Create a file called "
    "count.txt containing ONLY the number of lines that contain the word "
    '"ERROR" (case-sensitive), as a bare integer, no other text.'
)


def setup(workspace: Path) -> None:
    (workspace / "logs").mkdir()
    lines = []
    for i in range(200):
        if i % 7 == 3:  # deterministic ERROR placement → 29 lines
            lines.append(f"2026-01-01T00:0{i % 10}:00Z ERROR db timeout (retry {i})")
        elif i % 13 == 5:
            lines.append(f"2026-01-01T00:0{i % 10}:00Z WARN slow query {i}")
        else:
            lines.append(f"2026-01-01T00:0{i % 10}:00Z INFO request {i} ok")
    (workspace / "logs" / "app.log").write_text("\n".join(lines) + "\n")


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    log = workspace / "logs" / "app.log"
    expected = sum(1 for ln in log.read_text().splitlines() if "ERROR" in ln)
    path = workspace / "count.txt"
    if not path.exists():
        return False, "count.txt not created"
    value = path.read_text().strip()
    if value != str(expected):
        return False, f"expected {expected}, got {value!r}"
    return True, f"count matches ({expected})"


def SOLVE(workspace: Path) -> None:
    log = workspace / "logs" / "app.log"
    n = sum(1 for ln in log.read_text().splitlines() if "ERROR" in ln)
    (workspace / "count.txt").write_text(f"{n}\n")
