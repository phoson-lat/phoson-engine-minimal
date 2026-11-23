import asyncio

import pytest

from phoson_bridge.server import Bridge


@pytest.fixture
def bridge():
    instance = Bridge.__new__(Bridge)
    return instance


@pytest.mark.parametrize(
    "method,params",
    [
        ("_memory_list", {}),
        ("_memory_read", {"key": "x"}),
        ("_memory_write", {"key": "x", "content": "hello"}),
        ("_memory_delete", {"key": "x"}),
    ],
)
def test_workspace_required(bridge, method, params):
    with pytest.raises(ValueError, match="workspace explícito"):
        asyncio.run(getattr(bridge, method)(params))


def test_markdown_memory_crud_is_workspace_scoped(bridge, tmp_path, monkeypatch):
    from phoson_cli import memory

    monkeypatch.setattr(memory, "memory_root", lambda home=None: tmp_path / "memories")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert asyncio.run(
        bridge._memory_write({"workspace": str(a), "key": "notes", "content": "# A"})
    ) == {"written": True, "key": "notes"}
    assert asyncio.run(bridge._memory_list({"workspace": str(a)}))["items"] == [
        {"key": "notes", "content": "# A\n"}
    ]
    with pytest.raises(FileNotFoundError):
        asyncio.run(bridge._memory_read({"workspace": str(b), "key": "notes"}))
    assert (
        asyncio.run(bridge._memory_read({"workspace": str(a), "key": "notes"}))[
            "content"
        ]
        == "# A\n"
    )
    assert asyncio.run(bridge._memory_delete({"workspace": str(a), "key": "notes"}))[
        "deleted"
    ]
    assert asyncio.run(bridge._memory_list({"workspace": str(a)}))["items"] == []


def test_workspace_must_be_existing_directory(bridge, tmp_path):
    with pytest.raises((FileNotFoundError, ValueError)):
        asyncio.run(bridge._memory_list({"workspace": str(tmp_path / "missing")}))


def test_global_memory_crud_does_not_require_workspace(bridge, tmp_path, monkeypatch):
    from phoson_cli import memory

    monkeypatch.setattr(memory, "memory_root", lambda home=None: tmp_path / "memories")
    params = {"scope": "global", "key": "shared", "content": "Across projects"}
    assert asyncio.run(bridge._memory_write(params)) == {"written": True, "key": "shared"}
    assert asyncio.run(bridge._memory_list({"scope": "global"}))["items"] == [
        {"key": "shared", "content": "Across projects\n"}
    ]
    assert asyncio.run(bridge._memory_read({"scope": "global", "key": "shared"}))["content"] == "Across projects\n"
    assert asyncio.run(bridge._memory_delete({"scope": "global", "key": "shared"}))["deleted"]
    assert asyncio.run(bridge._memory_list({"scope": "global"}))["items"] == []


def test_memory_scope_is_validated(bridge):
    with pytest.raises(ValueError, match="scope"):
        asyncio.run(bridge._memory_list({"scope": "invalid"}))
