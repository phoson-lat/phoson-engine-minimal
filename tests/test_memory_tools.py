from pathlib import Path

import pytest

from phoson_agent.memory_tools import memory_tools


def test_memory_tools_scopes_and_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tools = {item.name: item for item in memory_tools()}
    workspace = str(tmp_path / "workspace")
    Path(workspace).mkdir()

    assert tools["memory_list"].handler({}, {"workspace": workspace}) == []
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    tools["memory_write"].handler(
        {"name": "note", "content": "other project"},
        {"workspace": str(other_workspace)},
    )
    assert tools["memory_list"].handler({}, {"workspace": workspace}) == []
    tools["memory_write"].handler(
        {"name": "note", "content": "remember this"}, {"workspace": workspace}
    )
    assert (
        tools["memory_read"].handler(
            {"name": "note"}, {"workspace": str(other_workspace)}
        )
        == "other project\n"
    )
    assert (
        tools["memory_read"].handler({"name": "note"}, {"workspace": workspace})
        == "remember this\n"
    )
    assert tools["memory_list"].handler({}, {"workspace": workspace}) == ["note"]
    assert (
        tools["memory_read"].handler(
            {"name": "note", "scope": "global"}, {"workspace": workspace}
        )
        == "Memory not found."
    )
    tools["memory_delete"].handler({"name": "note"}, {"workspace": workspace})
    assert tools["memory_list"].handler({}, {"workspace": workspace}) == []


def test_memory_rejects_secrets_and_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tools = {item.name: item for item in memory_tools()}
    with pytest.raises(ValueError):
        tools["memory_write"].handler(
            {"name": "x", "content": "api_key=abc"}, {"workspace": str(tmp_path)}
        )
    with pytest.raises(ValueError):
        tools["memory_read"].handler(
            {"name": "../escape"}, {"workspace": str(tmp_path)}
        )
