"""Parsing task: extract values from a noisy KEY=VALUE config.

Comments (#), blank lines, inline comments, and a red-herring KEY that
is commented out must be ignored; the checker recomputes the expected
map from the generated file, so the task can't silently change.
"""

from pathlib import Path

NAME = "parse-noisy-config"

# The authoritative pairs the file actually contains (comments excluded).
PAIRS = {
    "host": "0.0.0.0",
    "port": "8080",
    "workers": "4",
    "debug": "false",
    "timeout": "30",
}


def setup(workspace: Path) -> None:
    lines = [
        "# demo service config",
        "",
        "host = 0.0.0.0",
        "port=8080  # main port",  # inline comment, no space after =
        "workers = 4",
        "# host = 127.0.0.1",  # red herring: commented out
        "debug=false",
        "",
        "timeout = 30",
        "   ",  # whitespace-only line
        "# workers = 1",  # red herring: commented out
    ]
    (workspace / "service.cfg").write_text("\n".join(lines) + "\n")


INSTRUCTION = (
    "The file service.cfg is a KEY = VALUE config. Some lines are "
    "comments (start with #), some are blank, some have inline comments "
    "(after #), and some keys are commented out. Create a file called "
    "parsed.json containing a JSON object mapping each *active* key to "
    "its value, both as strings (strip whitespace, drop any inline "
    "comment after the value). Commented-out and blank lines must be "
    "ignored."
)


def _active_pairs(workspace: Path) -> dict[str, str]:
    """Parse service.cfg into the map of *active* key→value (comments,
    blanks, and commented-out keys excluded). Shared by check and SOLVE."""
    pairs: dict[str, str] = {}
    for raw in (workspace / "service.cfg").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        pairs[key.strip()] = val.split("#", 1)[0].strip()
    return pairs


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    import json

    # Recompute the expected map from the file (robust to task edits).
    expected = _active_pairs(workspace)
    assert expected == PAIRS, f"task bug: computed {expected} != planned {PAIRS}"

    path = workspace / "parsed.json"
    if not path.exists():
        return False, "parsed.json not created"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    if data != expected:
        return False, f"content mismatch: {data}"
    return True, "parsed active keys correctly, ignored comments"


def SOLVE(workspace: Path) -> None:
    import json

    (workspace / "parsed.json").write_text(json.dumps(_active_pairs(workspace)))
