"""Isolated plugin installs: stage → review → commit, prune, uninstall, update.

The installer subprocess is replaced by a fake that writes a real installed
distribution (dist-info with METADATA, entry_points.txt and RECORD, plus the
package and its phoson-plugin.toml) into the ``--target`` directory, so the
manager's metadata inspection runs against genuine importlib.metadata data.
"""

import sys
import textwrap
from types import SimpleNamespace
from pathlib import Path

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.plugin_manager import (
    PluginManagerError,
    plugin_info,
    plugins_dir,
    update_plugin,
    _load_lockfile,
    _save_lockfile,
    install_plugin,
    uninstall_plugin,
    activate_plugin_paths,
)

MANIFEST = """\
schema = 1
id = "{id}"
version = "{version}"
entry = "{module}:plugin"
description = "Demo plugin"
[compat]
phoson = "{compat}"
[provides]
tools = ["demo_tool"]
[capabilities]
network = true
"""

PLUGIN_CODE = """\
from phoson_agent import Plugin

class Demo(Plugin):
    @property
    def name(self):
        return "{id}"

plugin = Demo()
"""


def _write_dist(
    target: Path,
    name: str,
    version: str,
    *,
    module: str,
    entry_point: str | None = None,
    manifest: str | None = None,
) -> None:
    pkg = target / module
    pkg.mkdir(parents=True, exist_ok=True)
    files = [f"{module}/__init__.py"]
    (pkg / "__init__.py").write_text(
        PLUGIN_CODE.format(id=entry_point or name), encoding="utf-8"
    )
    if manifest is not None:
        (pkg / "phoson-plugin.toml").write_text(manifest, encoding="utf-8")
        files.append(f"{module}/phoson-plugin.toml")
    info = target / f"{module}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    if entry_point:
        (info / "entry_points.txt").write_text(
            f"[phoson.plugins]\n{entry_point} = {module}:plugin\n", encoding="utf-8"
        )
    record = [*files, f"{info.name}/METADATA", f"{info.name}/RECORD"]
    if entry_point:
        record.append(f"{info.name}/entry_points.txt")
    (info / "RECORD").write_text("".join(f"{f},,\n" for f in record), encoding="utf-8")


def fake_installer(
    *,
    plugin_id: str = "demo",
    version: str = "1.0.0",
    manifest_id: str | None = None,
    compat: str = ">=0",
    with_manifest: bool = True,
    host_copy: str | None = None,
    calls: list[list[str]] | None = None,
):
    def runner(command, **_kwargs):
        if calls is not None:
            calls.append(command)
        target = Path(command[command.index("--target") + 1])
        module = f"pi_{plugin_id.replace('-', '_')}"
        manifest = (
            MANIFEST.format(
                id=manifest_id or plugin_id,
                version=version,
                module=module,
                compat=compat,
            )
            if with_manifest
            else None
        )
        _write_dist(
            target,
            f"phoson-plugin-{plugin_id}",
            version,
            module=module,
            entry_point=plugin_id,
            manifest=manifest,
        )
        if host_copy:
            _write_dist(target, host_copy, "9.9.9", module=host_copy.replace("-", "_"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_PLUGINS_DIR", raising=False)
    monkeypatch.setattr("phoson_cli.plugin_manager.save_config", lambda *a, **k: None)
    monkeypatch.setattr("phoson_cli.plugin_manager.shutil.which", lambda _n: "uv")
    original_path = list(sys.path)
    yield home
    sys.path[:] = original_path
    for name in [m for m in sys.modules if m.startswith("pi_")]:
        del sys.modules[name]


def test_install_stages_reviews_and_commits_into_its_own_directory(env) -> None:
    config = PhosonConfig()
    calls: list[list[str]] = []
    reviews = []

    plugin_id = install_plugin(
        "phoson-plugin-demo==1.0.0",
        config,
        runner=fake_installer(calls=calls),
        confirm=lambda review: reviews.append(review) or True,
    )

    assert plugin_id == "demo"
    command = calls[0]
    assert command[:4] == ["uv", "pip", "install", "--target"]
    assert "--python" in command and command[-1] == "phoson-plugin-demo==1.0.0"
    assert "--constraint" in command  # host versions are pinned

    final = plugins_dir() / "demo"
    assert (final / "pi_demo" / "phoson-plugin.toml").is_file()
    assert not any(p.name.startswith(".staging-") for p in plugins_dir().iterdir())
    assert config.plugins == ["entrypoint:demo"]
    entry = _load_lockfile()[0]
    assert entry["location"] == str(final)
    assert entry["version"] == "1.0.0"
    assert entry["distribution"] == "phoson-plugin-demo"

    review = reviews[0]
    assert review.manifest is not None
    text = "\n".join(review.lines())
    assert "network access" in text and "tools: demo_tool" in text

    # The isolated directory is importable and its entry point visible.
    assert str(final) in sys.path
    info = plugin_info("demo", config)
    assert info.manifest is not None and info.manifest.id == "demo"


def test_declining_the_review_installs_nothing(env) -> None:
    config = PhosonConfig()
    result = install_plugin(
        "phoson-plugin-demo", config, runner=fake_installer(), confirm=lambda _r: False
    )
    assert result is None
    assert config.plugins == []
    assert _load_lockfile() == []
    assert list(plugins_dir().iterdir()) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"manifest_id": "other"}, "does not match"),
        ({"compat": ">=999"}, "not compatible"),
    ],
)
def test_invalid_plugins_are_rejected_and_cleaned_up(env, kwargs, message) -> None:
    config = PhosonConfig()
    with pytest.raises(PluginManagerError, match=message):
        install_plugin("phoson-plugin-demo", config, runner=fake_installer(**kwargs))
    assert config.plugins == []
    assert list(plugins_dir().iterdir()) == []


def test_host_packages_are_pruned_from_the_plugin_directory(env) -> None:
    config = PhosonConfig()
    install_plugin(
        "phoson-plugin-demo",
        config,
        runner=fake_installer(host_copy="packaging"),
    )
    final = plugins_dir() / "demo"
    assert not (final / "packaging").exists()
    assert not list(final.glob("packaging-*.dist-info"))
    assert (final / "pi_demo").is_dir()


def test_manifestless_plugin_installs_with_a_warning_in_review(env) -> None:
    config = PhosonConfig()
    seen = []
    install_plugin(
        "phoson-plugin-demo",
        config,
        runner=fake_installer(with_manifest=False),
        confirm=lambda review: seen.append(review) or True,
    )
    assert seen[0].manifest is None
    assert "no phoson-plugin.toml" in "\n".join(seen[0].lines())


def test_uninstall_deletes_the_directory_config_and_lock(env) -> None:
    config = PhosonConfig()
    install_plugin("phoson-plugin-demo", config, runner=fake_installer())
    final = plugins_dir() / "demo"

    result = uninstall_plugin("demo", config)

    assert result.deleted == final
    assert not final.exists()
    assert config.plugins == [] and config.disabled_plugins == []
    assert _load_lockfile() == []
    assert str(final) not in sys.path


def test_uninstall_never_deletes_outside_the_plugins_dir(env, tmp_path) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    config = PhosonConfig(plugins=["entrypoint:demo"])
    _save_lockfile([{"id": "demo", "source": "x", "location": str(outside)}])

    result = uninstall_plugin("demo", config)

    assert result.deleted is None
    assert outside.is_dir()
    assert config.plugins == []


def test_update_reinstalls_from_the_recorded_source(env) -> None:
    config = PhosonConfig()
    install_plugin("github:org/demo@main", config, runner=_with_git(fake_installer()))
    config.plugins = [{"name": "entrypoint:demo", "config": {"keep": True}}]

    calls: list[list[str]] = []
    assert (
        update_plugin(
            "demo",
            config,
            runner=_with_git(fake_installer(version="2.0.0", calls=calls), "b" * 40),
        )
        == "demo"
    )

    assert calls[-1][-1] == "git+https://github.com/org/demo.git@" + "b" * 40
    entry = _load_lockfile()[0]
    assert entry["version"] == "2.0.0"
    assert entry["resolved_commit"] == "b" * 40
    # The user's dict spec (with its config) is kept, not duplicated.
    assert config.plugins == [{"name": "entrypoint:demo", "config": {"keep": True}}]


def test_activate_ignores_locations_outside_the_plugins_dir(env, tmp_path) -> None:
    _save_lockfile([{"id": "x", "location": str(tmp_path)}])
    assert activate_plugin_paths() == []
    assert str(tmp_path) not in sys.path


def test_frozen_binary_requires_uv(env, monkeypatch) -> None:
    monkeypatch.setattr("phoson_cli.plugin_manager.shutil.which", lambda _n: None)
    monkeypatch.setattr("phoson_cli.plugin_manager._is_frozen", lambda: True)
    with pytest.raises(PluginManagerError, match="requires `uv`"):
        install_plugin("phoson-plugin-demo", PhosonConfig(), runner=fake_installer())


def _with_git(runner, commit: str = "a" * 40):
    def wrapped(command, **kwargs):
        if command[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=f"{commit}\tHEAD\n", stderr="")
        return runner(command, **kwargs)

    return wrapped


def test_manifest_template_is_stable() -> None:
    # Guards the fixture itself: the manifest it writes must parse.
    import tomllib

    from phoson_agent import parse_manifest

    parse_manifest(
        tomllib.loads(
            textwrap.dedent(
                MANIFEST.format(id="demo", version="1.0", module="m", compat=">=0")
            )
        )
    )
