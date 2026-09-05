"""Easy task: create a file inside a not-yet-existing nested directory."""

import json
from pathlib import Path

NAME = "create-nested-file"
INSTRUCTION = (
    "Create the file deploy/config/production.json (the directories do "
    "not exist yet). It must contain a JSON object with exactly: "
    'environment set to "production", replicas set to 3 (a number, not a '
    'string), and features set to a list ["auth", "billing", "search"].'
)


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "deploy" / "config" / "production.json"
    if not path.exists():
        return False, "deploy/config/production.json was not created"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    expected = {
        "environment": "production",
        "replicas": 3,
        "features": ["auth", "billing", "search"],
    }
    if data != expected:
        return False, f"content mismatch: {data}"
    if not isinstance(data["replicas"], int):
        return False, "replicas must be a JSON number, not a string"
    return True, "nested file matches spec"


def SOLVE(workspace: Path) -> None:
    (workspace / "deploy" / "config").mkdir(parents=True, exist_ok=True)
    (workspace / "deploy" / "config" / "production.json").write_text(
        json.dumps(
            {
                "environment": "production",
                "replicas": 3,
                "features": ["auth", "billing", "search"],
            }
        )
    )
