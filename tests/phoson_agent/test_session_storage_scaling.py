"""Session storage at scale: one write per save with meta (R5), a fast
listing, and two processes on one session merging instead of losing turns
(R4)."""

import json
from pathlib import Path

import pytest

from phoson_llm.schemas import Message
from phoson_agent.sessions.models import ConversationTree
from phoson_agent.sessions.storage_jsonl import JsonlStorage, list_session_metas

pytestmark = pytest.mark.asyncio


def _tree(turns: int = 1) -> ConversationTree:
    tree = ConversationTree.new()
    parent = None
    for i in range(turns):
        node = tree.append(parent, Message(role="user", content=f"q{i}"))
        node = tree.append(node.id, Message(role="assistant", content=f"a{i}"))
        parent = node.id
    return tree


def _leaf(tree: ConversationTree) -> str:
    return max(tree.nodes.values(), key=lambda n: n.created_at).id


async def test_save_with_meta_writes_once_and_persists_meta(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree()
    writes: list[str] = []
    real_write = storage._write_atomic
    storage._write_atomic = lambda t, p: (
        writes.append(t.session_id),
        real_write(t, p),
    )[1]  # type: ignore[method-assign]
    loads: list[str] = []
    real_load = storage._load_sync
    storage._load_sync = lambda sid, **kw: (loads.append(sid), real_load(sid, **kw))[1]  # type: ignore[method-assign]

    await storage.save_with_meta(tree, {"total_cost_usd": 1.5, "status": "completed"})

    assert writes == [tree.session_id] and loads == []
    loaded = await JsonlStorage(tmp_path).load(tree.session_id)
    assert loaded.total_cost == 1.5 and loaded.status == "completed"


async def test_two_processes_on_one_session_keep_both_turns(tmp_path: Path) -> None:
    first = JsonlStorage(tmp_path)
    original = _tree()
    await first.save(original)

    # Two CLIs resume the same session (separate storage instances = separate
    # processes' view of the file).
    cli_a, cli_b = JsonlStorage(tmp_path), JsonlStorage(tmp_path)
    tree_a = await cli_a.load(original.session_id)
    tree_b = await cli_b.load(original.session_id)
    a_node = tree_a.append(_leaf(tree_a), Message(role="user", content="from A"))
    await cli_a.save(tree_a)
    b_node = tree_b.append(_leaf(tree_b), Message(role="user", content="from B"))
    await cli_b.save(tree_b)

    on_disk = await JsonlStorage(tmp_path).load(original.session_id)
    assert a_node.id in on_disk.nodes and b_node.id in on_disk.nodes
    assert a_node.id in tree_b.nodes  # B's in-memory tree learned A's branch


async def test_a_single_writer_never_rereads_the_file(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree()
    await storage.save(tree)
    loads: list[str] = []
    real_load = storage._load_sync
    storage._load_sync = lambda sid, **kw: (loads.append(sid), real_load(sid, **kw))[1]  # type: ignore[method-assign]
    for i in range(3):
        tree.append(_leaf(tree), Message(role="user", content=f"more {i}"))
        await storage.save(tree)
    assert loads == []


async def test_listing_reads_meta_and_counts_without_decoding(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree(turns=3)
    await storage.save_with_meta(tree, {"title": "T", "step_count": 4})
    (meta,) = list_session_metas(tmp_path)
    assert meta.message_count == 6 and meta.title == "T" and meta.step_count == 4
    first_node = min(tree.nodes.values(), key=lambda n: n.created_at)
    assert meta.created_at == first_node.created_at


async def test_legacy_files_without_a_leading_meta_still_list(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree(turns=2)
    await storage.save(tree)
    path = tmp_path / f"{tree.session_id}.jsonl"
    lines = path.read_text().splitlines()
    meta_line, rest = lines[0], lines[1:]
    assert json.loads(meta_line)["type"] == "session_meta"
    path.write_text("\n".join([*rest, meta_line]) + "\n")  # meta last (legacy)
    (meta,) = list_session_metas(tmp_path)
    assert meta.message_count == 4
