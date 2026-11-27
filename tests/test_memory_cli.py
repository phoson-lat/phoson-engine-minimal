"""CLI-level tests for the documented /memory command syntax."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from phoson_cli.commands import Command, CommandHandler


class Renderer:
    def __init__(self):
        self.info = []
        self.errors = []

    def print_info(self, message):
        self.info.append(message)

    def print_error(self, message):
        self.errors.append(message)


@pytest.mark.asyncio
async def test_memory_cli_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "workspace"
    project.mkdir()
    renderer = Renderer()
    session = SimpleNamespace(tree=SimpleNamespace(cwd=str(project)))
    owner = CommandHandler.__new__(CommandHandler)
    owner.session = session
    owner.host = renderer

    async def run(args):
        return await owner._cmd_memory(Command(name="/memory", args=args))

    assert await run("list") is True
    assert renderer.info[-1] == "project memories: (none)"
    await run("write project meeting Meeting notes for Monday")
    assert "Memory saved:" in renderer.info[-1]
    await run("read project meeting")
    assert renderer.info[-1] == "Meeting notes for Monday"
    await run("list project")
    assert renderer.info[-2:] == ["Project memories:", "  meeting"]
    await run("write global preference Use concise answers")
    await run("read global preference")
    assert renderer.info[-1] == "Use concise answers"
    await run("delete project meeting")
    assert renderer.info[-1] == "Memory deleted: meeting"


@pytest.mark.asyncio
async def test_memory_cli_reports_invalid_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "workspace"
    project.mkdir()
    renderer = Renderer()
    owner = CommandHandler.__new__(CommandHandler)
    owner.session = SimpleNamespace(tree=SimpleNamespace(cwd=str(project)))
    owner.host = renderer
    await owner._cmd_memory(Command(name="/memory", args="show project missing"))
    assert renderer.errors
