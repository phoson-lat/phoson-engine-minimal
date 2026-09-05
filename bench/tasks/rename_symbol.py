"""Coordinated rename: change a symbol name at its definition AND every
call site. A single-site edit (only the def, or only one caller) is
detectable via a stale-reference grep + a runtime check.
"""

import subprocess
from pathlib import Path

NAME = "rename-symbol"

SRC = {
    "api/users.py": (
        "def fetch_users(limit=10):\n    return ['u' + str(i) for i in range(limit)]\n"
    ),
    "api/views.py": (
        "from api.users import fetch_users\n\ndef show():\n    return fetch_users(5)\n"
    ),
    "tests/test_users.py": (
        "from api.users import fetch_users\n\n"
        "def test_it():\n"
        "    assert len(fetch_users(3)) == 3\n"
    ),
    "api/__init__.py": "",
}


def setup(workspace: Path) -> None:
    for rel, content in SRC.items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


INSTRUCTION = (
    "Rename the function `fetch_users` to `list_users` everywhere it is "
    "defined or used (its definition in api/users.py, plus every import "
    "and call site). After your change, importing and calling "
    "`list_users` must work, and no reference to the old name may remain."
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    proc = subprocess.run(
        [
            "python3",
            "-c",
            "from api.users import list_users; print(len(list_users(2)))",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return False, f"import still fails: {proc.stderr[:200]}"
    if proc.stdout.strip() != "2":
        return False, f"expected '2', got {proc.stdout.strip()!r}"
    # No stale reference to the old name anywhere.
    grep = subprocess.run(
        ["grep", "-r", "fetch_users", "."],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if grep.stdout.strip():
        return False, f"stale 'fetch_users' remains: {grep.stdout.strip()}"
    return True, "renamed at def and all call sites"


def SOLVE(workspace: Path) -> None:
    for rel in ("api/users.py", "api/views.py", "tests/test_users.py"):
        p = workspace / rel
        p.write_text(p.read_text().replace("fetch_users", "list_users"))
