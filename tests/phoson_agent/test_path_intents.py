"""Protective path intents: writes outside the workspace and credential reads
ask by default, resolved exactly like the file tools resolve their paths."""

import os
from pathlib import Path

import pytest

from phoson_agent.intents import (
    INTENT_FILESYSTEM_READ_SENSITIVE,
    INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE,
    infer_intents,
)
from phoson_agent.permissions import PermissionPolicy


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("secret")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    return project


def test_write_inside_workspace_is_unchanged(workspace: Path) -> None:
    assert infer_intents("write_file", {"path": "src/a.py"}) == ("filesystem_write",)
    assert PermissionPolicy().check("write_file", "src/a.py", {"path": "src/a.py"}) == (
        "allow"
    )


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/hosts-copy", "{home}/.bashrc"],
)
def test_write_outside_workspace_asks(workspace: Path, path: str) -> None:
    path = path.format(home=os.environ["HOME"])
    intents = infer_intents("patch_file", {"path": path})
    assert INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE in intents
    assert PermissionPolicy().check("patch_file", path, {"path": path}) == "ask"


def test_tilde_is_literal_for_write_file(workspace: Path) -> None:
    # write_file uses Path(path) verbatim: "~/x" lands in ./~/x, inside cwd.
    assert infer_intents("write_file", {"path": "~/x"}) == ("filesystem_write",)


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlink_escaping_workspace_asks(workspace: Path) -> None:
    (workspace / "link").symlink_to(Path(os.environ["HOME"]))
    intents = infer_intents("write_file", {"path": "link/.zshrc"})
    assert INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE in intents


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("read_file", "{home}/.ssh/id_rsa"),
        ("view_image", "~/.ssh/id_rsa"),
        ("grep", "~"),
        ("list_dir", "{home}"),
    ],
)
def test_sensitive_reads_ask(workspace: Path, tool: str, path: str) -> None:
    path = path.format(home=os.environ["HOME"])
    assert INTENT_FILESYSTEM_READ_SENSITIVE in infer_intents(tool, {"path": path})
    assert PermissionPolicy().check(tool, path, {"path": path}) == "ask"


def test_regular_reads_are_unchanged(workspace: Path) -> None:
    assert infer_intents("read_file", {"path": "README.md"}) == ("filesystem_read",)
    assert infer_intents("grep", {"pattern": "x"}) == ("filesystem_read",)


def test_user_can_opt_out_and_auto_mode_does_not_bypass(workspace: Path) -> None:
    path = "../outside.txt"
    auto = PermissionPolicy(levels={"*": "allow"})
    assert auto.check("write_file", path, {"path": path}) == "ask"

    opted_out = PermissionPolicy(
        intent_levels={INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE: "allow"}
    )
    assert opted_out.check("write_file", path, {"path": path}) == "allow"

    pattern = PermissionPolicy(allow_patterns={"write_file": ["../*"]})
    assert pattern.check("write_file", path, {"path": path}) == "allow"
