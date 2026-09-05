"""Coordinated multi-file edit: bump a version constant in 3 files.

The change must land in exactly the three places, leaving every other
byte of those files untouched — a single-file-only fix is detectable.
"""

import subprocess
from pathlib import Path

NAME = "bump-version-files"

FILES = {
    "config/app.json": '{"app": "demo", "version": "1.2.0"}\n',
    "src/meta.py": 'VERSION = "1.2.0"\n\n\ndef show():\n    return VERSION\n',
    "README.md": "# Demo app\n\nCurrent version: 1.2.0\n",
}


def setup(workspace: Path) -> None:
    for rel, content in FILES.items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


INSTRUCTION = (
    "This project is at version 1.2.0. Bump it to 1.3.0 everywhere the "
    'string "1.2.0" appears (config/app.json, src/meta.py, README.md). '
    "Do not change anything else in those files."
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    expected = {rel: c.replace("1.2.0", "1.3.0") for rel, c in FILES.items()}
    for rel, want in expected.items():
        p = workspace / rel
        if not p.exists():
            return False, f"{rel} missing"
        got = p.read_text()
        if got != want:
            return False, f"{rel} mismatch: {got!r}"
    # And no other file may still carry the old version.
    proc = subprocess.run(
        ["grep", "-r", "1.2.0", "."],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        return False, f"stale 1.2.0 remains: {proc.stdout.strip()}"
    return True, "version bumped in all three files, nothing else touched"


def SOLVE(workspace: Path) -> None:
    """Write the bumped content directly (idempotent, order-independent)."""
    for rel, content in FILES.items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.replace("1.2.0", "1.3.0"))
