"""Create a file with exact JSON content from a natural-language spec."""

import json
from pathlib import Path

NAME = "create-json-config"
INSTRUCTION = (
    "Create a file named config.json in the current directory containing "
    'a JSON object with keys: name set to "phoson", version set to '
    '"1.0.0", and tags set to a list ["agent", "cli"].'
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "config.json"
    if not path.exists():
        return False, "config.json was not created"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    expected = {"name": "phoson", "version": "1.0.0", "tags": ["agent", "cli"]}
    if data != expected:
        return False, f"content mismatch: {data}"
    return True, "config.json matches spec"


def setup(workspace: Path) -> None:
    pass
