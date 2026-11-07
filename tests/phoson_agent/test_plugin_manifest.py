"""phoson-plugin.toml parsing, validation, config resolution and loader use."""

import sys
import textwrap
from pathlib import Path

import pytest

from phoson_agent.exceptions import PhosonPluginLoadError
from phoson_agent.plugin_loader import load_plugin, find_plugin_manifest
from phoson_agent.plugin_manifest import (
    MANIFEST_FILENAME,
    PluginManifestError,
    load_manifest,
    parse_manifest,
    find_manifest_in_tree,
    find_manifest_for_module,
)

MINIMAL = {"schema": 1, "id": "demo", "version": "1.0.0", "entry": "demo_pkg:plugin"}


def test_minimal_manifest_parses_with_defaults() -> None:
    manifest = parse_manifest(MINIMAL)
    assert manifest.id == "demo"
    assert manifest.entry_module == "demo_pkg"
    assert manifest.entry_attribute == "plugin"
    assert manifest.capabilities.filesystem == "none"
    assert manifest.provides.tools == ()


def test_all_problems_are_reported_at_once() -> None:
    with pytest.raises(PluginManifestError) as info:
        parse_manifest(
            {
                "schema": 2,
                "id": "Bad_Id",
                "version": "not a version",
                "entry": "1bad",
                "extra": True,
                "compat": {"phoson": ">>1"},
                "provides": {"commands": ["nolash"]},
                "capabilities": {"filesystem": "everything"},
                "config": {"k": {"type": "float"}},
            }
        )
    problems = "\n".join(info.value.problems)
    for fragment in (
        "schema: 2",
        "id:",
        "version:",
        "entry:",
        "unknown top-level key 'extra'",
        "compat.phoson",
        "must start with '/'",
        "capabilities.filesystem",
        "config.k.type",
    ):
        assert fragment in problems


def test_secret_with_default_is_rejected() -> None:
    with pytest.raises(PluginManifestError, match="secret must not ship a default"):
        parse_manifest(
            {**MINIMAL, "config": {"token": {"secret": True, "default": "x"}}}
        )


def _with_config() -> dict:
    return {
        **MINIMAL,
        "config": {
            "url": {"type": "string", "default": "https://x"},
            "token": {"type": "string", "secret": True, "env": "DEMO_TOKEN"},
            "retries": {"type": "integer", "default": 3},
            "mode": {"type": "string", "choices": ["a", "b"], "default": "a"},
            "verbose": {"type": "boolean", "env": "DEMO_VERBOSE"},
            "region": {"type": "string", "required": True},
        },
    }


def test_resolve_config_fills_defaults_env_and_validates() -> None:
    manifest = parse_manifest(_with_config())
    resolved = manifest.resolve_config(
        {"region": "mx", "retries": 5},
        environ={"DEMO_TOKEN": "t0k", "DEMO_VERBOSE": "yes"},
    )
    assert resolved == {
        "url": "https://x",
        "token": "t0k",
        "retries": 5,
        "mode": "a",
        "verbose": True,
        "region": "mx",
    }


def test_resolve_config_reports_every_problem() -> None:
    manifest = parse_manifest(_with_config())
    with pytest.raises(PluginManifestError) as info:
        manifest.resolve_config(
            {"retries": "3", "mode": "c", "typo": 1},
            environ={"DEMO_VERBOSE": "maybe"},
        )
    problems = "\n".join(info.value.problems)
    assert "unknown config key 'typo'" in problems
    assert "retries: expected integer" in problems
    assert "mode: 'c' is not one of" in problems
    assert "verbose: $DEMO_VERBOSE is not a boolean" in problems
    assert "region: required" in problems


def test_no_config_table_passes_config_through() -> None:
    manifest = parse_manifest(MINIMAL)
    assert manifest.resolve_config({"anything": 1}) == {"anything": 1}


@pytest.mark.parametrize(
    ("spec", "engine", "expected"),
    [
        (">=1.0", "1.2.0", None),
        (">=2", "1.2.0", "requires phoson >=2"),
        (">=2", None, None),
    ],
)
def test_incompatibility(spec: str, engine: str | None, expected: str | None) -> None:
    manifest = parse_manifest({**MINIMAL, "compat": {"phoson": spec}})
    reason = manifest.incompatibility(engine)
    assert (reason is None) if expected is None else expected in reason


def test_summary_mentions_capabilities_and_provides() -> None:
    manifest = parse_manifest(
        {
            **MINIMAL,
            "description": "Demo",
            "provides": {"tools": ["a"], "commands": ["/demo"]},
            "capabilities": {"shell": True, "network": True},
        }
    )
    text = "\n".join(manifest.summary_lines())
    assert "runs shell commands" in text
    assert "network access" in text
    assert "tools: a" in text
    assert "commands: /demo" in text


# ── Discovery and loader integration ─────────────────────────────────────────

PLUGIN_SOURCE = textwrap.dedent(
    """
    from phoson_agent import Plugin

    class Demo(Plugin):
        configured = None

        @property
        def name(self):
            return "{name}"

        def configure(self, config):
            self.configured = config

        def initialize(self):
            raise AssertionError("initialize must not run")

    plugin = Demo()
    """
)


def _make_package(
    root: Path, package: str, manifest: str | None, name: str = "demo"
) -> Path:
    pkg = root / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(PLUGIN_SOURCE.format(name=name))
    if manifest is not None:
        (pkg / MANIFEST_FILENAME).write_text(textwrap.dedent(manifest))
    return pkg


@pytest.fixture
def on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for module in [m for m in sys.modules if m.startswith("mf_pkg")]:
        del sys.modules[module]


def test_manifest_is_found_without_importing(on_path: Path) -> None:
    _make_package(
        on_path,
        "mf_pkg_find",
        """
        schema = 1
        id = "demo"
        version = "1.0"
        entry = "mf_pkg_find:plugin"
        """,
    )
    path = find_manifest_for_module("mf_pkg_find.sub")
    assert path is not None and load_manifest(path).id == "demo"
    assert "mf_pkg_find" not in sys.modules
    assert find_manifest_in_tree(on_path) is not None


def test_loader_applies_manifest_config_and_attaches_it(on_path: Path) -> None:
    _make_package(
        on_path,
        "mf_pkg_load",
        """
        schema = 1
        id = "demo"
        version = "1.0"
        entry = "mf_pkg_load:plugin"
        [config.level]
        type = "integer"
        default = 2
        """,
    )
    plugin = load_plugin({"name": "mf_pkg_load", "config": {}}, initialize=False)
    assert plugin.configured == {"level": 2}
    assert plugin.manifest is not None and plugin.manifest.id == "demo"
    assert find_plugin_manifest("mf_pkg_load").id == "demo"


def test_loader_rejects_bad_config(on_path: Path) -> None:
    _make_package(
        on_path,
        "mf_pkg_badcfg",
        """
        schema = 1
        id = "demo"
        version = "1.0"
        entry = "mf_pkg_badcfg:plugin"
        [config.level]
        type = "integer"
        """,
    )
    with pytest.raises(PluginManifestError, match="level: expected integer"):
        load_plugin({"name": "mf_pkg_badcfg", "config": {"level": "x"}})


def test_incompatible_plugin_is_refused_before_import(on_path: Path) -> None:
    _make_package(
        on_path,
        "mf_pkg_incompat",
        """
        schema = 1
        id = "demo"
        version = "1.0"
        entry = "mf_pkg_incompat:plugin"
        [compat]
        phoson = ">=999"
        """,
    )
    with pytest.raises(PhosonPluginLoadError, match="not compatible"):
        load_plugin("mf_pkg_incompat")
    assert "mf_pkg_incompat" not in sys.modules


def test_name_mismatch_warns(on_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _make_package(
        on_path,
        "mf_pkg_mismatch",
        """
        schema = 1
        id = "other"
        version = "1.0"
        entry = "mf_pkg_mismatch:plugin"
        """,
    )
    load_plugin("mf_pkg_mismatch", initialize=False)
    assert "should match the manifest id" in caplog.text


def test_plugin_without_manifest_loads_unchanged(on_path: Path) -> None:
    _make_package(on_path, "mf_pkg_plain", None)
    plugin = load_plugin({"name": "mf_pkg_plain", "config": {"x": 1}}, initialize=False)
    assert plugin.configured == {"x": 1}
    assert plugin.manifest is None
