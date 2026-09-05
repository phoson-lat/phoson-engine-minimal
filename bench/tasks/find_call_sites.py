"""Search task: find every call site of a function scattered across a repo.

Requires repo-wide content search (grep/rg) to locate the usages — the
definition lives in one file, the calls in several others, in no
particular order, with surrounding noise.
"""

from pathlib import Path

NAME = "find-call-sites"
INSTRUCTION = (
    "In this project the function `normalize_id` is defined in "
    "lib/utils.py. Several other modules call it. Create a file named "
    "call_sites.txt containing ONLY the relative paths of the files that "
    "call `normalize_id` (not the definition file itself), one path per "
    "line, sorted alphabetically, no other text."
)


def setup(workspace: Path) -> None:
    (workspace / "lib").mkdir()
    (workspace / "lib" / "utils.py").write_text(
        "def normalize_id(value):\n"
        '    """Canonicalize an identifier."""\n'
        "    return str(value).strip().lower()\n",
        encoding="utf-8",
    )

    # Call sites, scattered and unsorted on purpose.
    (workspace / "web").mkdir()
    (workspace / "web" / "views.py").write_text(
        "from lib.utils import normalize_id\n\n"
        "def handle(raw):\n"
        "    key = normalize_id(raw)\n"
        "    return key\n",
        encoding="utf-8",
    )
    (workspace / "core").mkdir()
    (workspace / "core" / "store.py").write_text(
        "import lib.utils as utils\n\n"
        "def fetch(raw):\n"
        "    return _table[utils.normalize_id(raw)]\n"
        "_table = {}\n",
        encoding="utf-8",
    )
    (workspace / "jobs").mkdir()
    (workspace / "jobs" / "etl.py").write_text(
        "from lib.utils import normalize_id\n\n"
        "def run(rows):\n"
        "    seen = set()\n"
        "    for r in rows:\n"
        "        seen.add(normalize_id(r))\n"
        "    return seen\n",
        encoding="utf-8",
    )

    # Noise: a comment mentioning the name, and a similarly-named fn.
    (workspace / "README.md").write_text(
        "TODO: normalize_id should maybe be renamed.\n", encoding="utf-8"
    )
    (workspace / "lib" / "names.py").write_text(
        "def normalize_name(value):\n    return value\n", encoding="utf-8"
    )


def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    path = workspace / "call_sites.txt"
    if not path.exists():
        return False, "call_sites.txt not created"
    got = sorted(line.strip() for line in path.read_text().splitlines() if line.strip())
    expected = ["core/store.py", "jobs/etl.py", "web/views.py"]
    if got != expected:
        return False, f"expected {expected}, got {got}"
    return True, "all call sites found"
