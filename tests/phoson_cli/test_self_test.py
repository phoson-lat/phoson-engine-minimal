"""``phoson-cli --self-test`` passes from source and covers every official plugin."""

import io
from pathlib import Path

from phoson_cli.self_test import OFFICIAL_PLUGIN_PACKAGES, run_self_test

ROOT = Path(__file__).resolve().parents[2]


def test_self_test_passes_from_source() -> None:
    out = io.StringIO()
    assert run_self_test(out) == 0, out.getvalue()
    text = out.getvalue()
    assert "ok    agent turn (fake model, tool call)" in text
    assert "ok    tokenizer cl100k_base" in text
    assert "FAIL" not in text


def test_official_plugin_list_matches_the_repository() -> None:
    on_disk = sorted(p.name for p in ROOT.glob("phoson_plugin_*") if p.is_dir())
    assert sorted(OFFICIAL_PLUGIN_PACKAGES) == on_disk


def test_bundle_spec_ships_tiktoken_plugins_and_runtime_hook() -> None:
    spec = (ROOT / "phoson_cli.spec").read_text(encoding="utf-8")
    assert 'collect_submodules("tiktoken_ext")' in spec
    assert 'collect_data_files("tiktoken_ext", include_py_files=True)' in spec
    assert "pyi_rth_phoson.py" in spec
    assert (ROOT / "scripts" / "pyi_rth_phoson.py").is_file()


def test_release_workflow_runs_the_self_test() -> None:
    workflow = ROOT / ".github" / "workflows" / "release-binaries.yml"
    assert '"$exe" --self-test' in workflow.read_text(encoding="utf-8")
