"""``plugin new`` scaffolds a valid, self-testing plugin; ``plugin validate``
catches manifest/packaging mistakes, with and without importing."""

import os
import sys
import subprocess
from pathlib import Path

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.__main__ import _run_plugin_command
from phoson_cli.plugin_dev import (
    PluginDevError,
    scaffold_plugin,
    validate_plugin_source,
)


@pytest.fixture(autouse=True)
def _forget_scaffolded_modules():
    yield
    for name in [m for m in sys.modules if m.startswith("phoson_plugin_acme")]:
        del sys.modules[name]


def test_scaffold_is_valid_with_and_without_loading(tmp_path: Path) -> None:
    root = scaffold_plugin("acme-notes", tmp_path)

    assert root == tmp_path / "phoson-plugin-acme-notes"
    report = validate_plugin_source(root)
    assert report.ok, report.errors
    assert report.manifest is not None and report.manifest.id == "acme-notes"
    loaded = validate_plugin_source(root, load=True)
    assert loaded.ok, loaded.errors


def test_scaffold_tests_pass_on_their_own(tmp_path: Path) -> None:
    root = scaffold_plugin("acme-tested", tmp_path)
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root)],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("bad", ["Acme", "acme_notes", "-acme", "acme--x"])
def test_scaffold_rejects_bad_ids(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PluginDevError, match="Invalid plugin id"):
        scaffold_plugin(bad, tmp_path)


def test_scaffold_refuses_to_overwrite(tmp_path: Path) -> None:
    scaffold_plugin("acme-once", tmp_path)
    with pytest.raises(PluginDevError, match="already exists"):
        scaffold_plugin("acme-once", tmp_path)


def _replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text().replace(old, new))


def test_validate_reports_packaging_mistakes(tmp_path: Path) -> None:
    root = scaffold_plugin("acme-broken", tmp_path)
    _replace(
        root / "pyproject.toml",
        'acme-broken = "phoson_plugin_acme_broken:create_plugin"',
        'other = "phoson_plugin_acme_broken:create_plugin"',
    )
    _replace(root / "pyproject.toml", 'version = "0.1.0"', 'version = "0.2.0"')

    report = validate_plugin_source(root)

    assert not report.ok
    assert any("must declare" in e for e in report.errors)
    assert any("extra 'phoson.plugins' entry points: other" in e for e in report.errors)
    assert any("differs from pyproject version 0.2.0" in w for w in report.warnings)


def test_validate_load_detects_undeclared_tools(tmp_path: Path) -> None:
    root = scaffold_plugin("acme-drift", tmp_path)
    manifest = root / "src" / "phoson_plugin_acme_drift" / "phoson-plugin.toml"
    _replace(manifest, 'tools = ["acme_drift_hello"]', 'tools = ["renamed"]')

    assert validate_plugin_source(root).ok  # static checks cannot see it
    report = validate_plugin_source(root, load=True)
    assert any("declared tools not provided: renamed" in e for e in report.errors)
    assert any(
        "not declared in the manifest: acme_drift_hello" in e for e in report.errors
    )


def test_validate_without_manifest(tmp_path: Path) -> None:
    report = validate_plugin_source(tmp_path)
    assert not report.ok and "no phoson-plugin.toml" in report.errors[0]


def test_cli_new_and_validate(tmp_path: Path, capsys) -> None:
    config = PhosonConfig()
    assert _run_plugin_command(["new", "acme-cli", "--dir", str(tmp_path)], config) == 0
    root = tmp_path / "phoson-plugin-acme-cli"
    assert _run_plugin_command(["validate", str(root), "--load"], config) == 0
    assert "Plugin source is valid." in capsys.readouterr().out
    assert _run_plugin_command(["validate", str(tmp_path / "missing")], config) == 1


def test_cli_plugin_help(capsys) -> None:
    assert _run_plugin_command(["help"], PhosonConfig()) == 0
    out = capsys.readouterr().out
    for command in (
        "install",
        "uninstall",
        "update",
        "info",
        "doctor",
        "new",
        "validate",
    ):
        assert command in out
