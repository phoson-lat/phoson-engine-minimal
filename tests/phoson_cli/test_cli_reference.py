"""docs/cli/reference.md must match the CLI's --help text and COMMAND_SPECS."""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_cli_reference.py"


def test_cli_reference_is_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location("gen_cli_reference", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    committed = module.TARGET.read_text(encoding="utf-8")
    assert committed == module.render(), (
        "docs/cli/reference.md is stale: run "
        "`uv run python scripts/gen_cli_reference.py`"
    )
