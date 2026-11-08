"""Optional features installed on demand (``/extras``, ``phoson-cli extras``)."""

import sys
import json
import tomllib
import subprocess
from pathlib import Path

import pytest

from phoson_cli import extras
from phoson_cli.commands import Command, CommandHandler

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PHOSON_HOME", str(tmp_path / ".phoson"))
    # Never download uv in tests; the fake runner does the "install".
    monkeypatch.setattr(extras, "_ensure_uv", lambda notify: None)
    saved = list(sys.path)
    yield tmp_path
    sys.path[:] = saved


def test_catalog_mirrors_pyproject_extras() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    covered: set[str] = set()
    for extra in extras.EXTRAS:
        sources = extra.pyproject or (extra.name,)
        expected = {req for name in sources for req in declared[name]}
        assert set(extra.requirements) == expected, extra.name
        covered.update(sources)
    assert covered == set(declared)


def test_unknown_extra_lists_the_known_ones() -> None:
    with pytest.raises(extras.ExtrasError, match="Available: .*stt"):
        extras.get_extra("nope")
    assert extras.get_extra(" STT ").name == "stt"


def _fake_runner(module: str, dist: str = "fakepkg", version: str = "1.0"):
    """A runner that "installs" *module* into the ``--target`` directory."""
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        package = target / module.split(".")[0]
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 42\n")
        info = target / f"{dist}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
        )
        (target / ".lock").write_text("")
        return subprocess.CompletedProcess(command, 0, "", "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_install_targets_a_private_dir_and_activates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = extras.Extra("demo", "Demo", ("fakepkg>=1",), ("phoson_fake_extra_mod",))
    monkeypatch.setattr(extras, "_BY_NAME", {"demo": fake})
    monkeypatch.setattr(extras, "EXTRAS", (fake,))
    runner = _fake_runner("phoson_fake_extra_mod")
    messages: list[str] = []

    status = extras.install_extra("demo", runner=runner, notify=messages.append)

    command = runner.calls[0]
    target = extras.extras_dir() / "demo"
    assert "--target" in command and command[-1] == "fakepkg>=1"
    assert "--constraint" in command  # the CLI's packages are pinned
    assert status.state == "installed" and status.ready
    assert str(target) == sys.path[-1]  # appended: the CLI's packages win
    assert not (target / ".lock").exists()
    record = json.loads((target / extras.RECORD_FILE).read_text())
    assert record["packages"] == ["fakepkg==1.0"]
    assert record["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert any("Installing demo" in m for m in messages)
    assert not list(extras.extras_dir().glob(".staging-*"))
    assert not list(extras.extras_dir().glob("*.constraints.txt"))
    import phoson_fake_extra_mod  # type: ignore[import-not-found]

    assert phoson_fake_extra_mod.VALUE == 42

    # A new process finds it again; a different Python does not load it.
    sys.path.remove(str(target))
    assert extras.activate_extras() == [target]
    record["python"] = "2.7"
    (target / extras.RECORD_FILE).write_text(json.dumps(record))
    sys.path.remove(str(target))
    assert extras.activate_extras() == []
    assert extras.extra_status(fake).state == "stale"

    assert extras.remove_extra("demo") is True
    assert not target.exists()
    assert extras.remove_extra("demo") is False
    sys.modules.pop("phoson_fake_extra_mod", None)


def test_failed_install_keeps_nothing_and_reports_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "line\n" * 30 + "boom")

    with pytest.raises(extras.ExtrasError, match="boom") as info:
        extras.install_extra("ssh", runner=run, notify=lambda _m: None)
    assert str(info.value).count("line") <= 12
    assert not (extras.extras_dir() / "ssh").exists()
    assert not list(extras.extras_dir().glob(".staging-*"))


def test_platform_markers_decide_what_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt = extras.get_extra("stt")
    monkeypatch.setattr(sys, "platform", "darwin")
    import platform

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert stt.applicable_requirements() == []
    assert extras.extra_status(stt).state == "unsupported"
    with pytest.raises(extras.ExtrasError, match="not available on this platform"):
        extras.install_extra("stt", runner=_fake_runner("x"))


def test_cli_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    from phoson_cli.__main__ import parse_args

    assert parse_args(["extras", "install", "stt"]).extras_args == ["install", "stt"]
    assert extras.run_extras_command([]) == 0
    assert "stt" in capsys.readouterr().out
    assert extras.run_extras_command(["frobnicate"]) == 2
    assert extras.run_extras_command(["install", "nope"]) == 1
    assert "Unknown extra" in capsys.readouterr().err


async def test_slash_command_lists_and_reports_errors() -> None:
    class Host:
        def __init__(self) -> None:
            self.info: list[str] = []
            self.errors: list[str] = []

        def print_info(self, message: str) -> None:
            self.info.append(message)

        def print_error(self, message: str) -> None:
            self.errors.append(message)

    handler = CommandHandler.__new__(CommandHandler)
    handler.host = Host()  # type: ignore[assignment]
    await handler._cmd_extras(Command("/extras", ""))
    assert "computeruse" in handler.host.info[0]  # type: ignore[attr-defined]
    await handler._cmd_extras(Command("/extras", "install nope"))
    assert "Unknown extra" in handler.host.errors[0]  # type: ignore[attr-defined]


def test_install_reports_a_module_that_does_not_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = extras.Extra("demo", "Demo", ("fakepkg>=1",), ("phoson_fake_broken_mod",))
    monkeypatch.setattr(extras, "_BY_NAME", {"demo": fake})
    monkeypatch.setattr(extras, "EXTRAS", (fake,))

    def run(command, **_kwargs):
        target = Path(command[command.index("--target") + 1])
        (target / "phoson_fake_broken_mod").mkdir(parents=True)
        (target / "phoson_fake_broken_mod" / "__init__.py").write_text(
            "raise OSError('libportaudio not found')\n"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    status = extras.install_extra("demo", runner=run, notify=lambda _m: None)
    assert status.state == "broken" and not status.ready
    assert "libportaudio not found" in status.detail
    assert extras.run_extras_command(["remove", "demo"]) == 0
