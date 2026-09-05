"""Multi-step task: explore a small repo and fix an import error."""

import subprocess
from pathlib import Path

NAME = "fix-import-error"
INSTRUCTION = (
    "Running `python -m app.main` in this project fails with an import "
    "error. Investigate the project structure and fix it so the command "
    "prints 'ok' and exits 0."
)


def setup(workspace: Path) -> None:
    (workspace / "app").mkdir()
    (workspace / "app" / "__init__.py").write_text("")
    # main.py imports helpers.utils but the module is named util.py (typo)
    (workspace / "app" / "main.py").write_text(
        "from app.helpers.utils import greet\n\n"
        "if __name__ == '__main__':\n    print(greet())\n"
    )
    (workspace / "app" / "helpers").mkdir()
    (workspace / "app" / "helpers" / "__init__.py").write_text("")
    (workspace / "app" / "helpers" / "util.py").write_text(
        "def greet() -> str:\n    return 'ok'\n"
    )


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    proc = subprocess.run(
        ["python3", "-m", "app.main"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return False, f"still fails: {proc.stderr[:200]}"
    if proc.stdout.strip() != "ok":
        return False, f"expected 'ok', got {proc.stdout.strip()!r}"
    return True, "module runs correctly"


def SOLVE(workspace: Path) -> None:
    """The module is named util.py but main.py imports ``utils`` — rename it."""
    helpers = workspace / "app" / "helpers"
    (helpers / "util.py").rename(helpers / "utils.py")
