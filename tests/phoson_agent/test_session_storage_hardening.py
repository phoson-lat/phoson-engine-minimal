"""JsonlStorage hardening: partial meta updates, concurrent saves, privacy,
and tolerance of a damaged line."""

import os
import stat
import asyncio
from pathlib import Path

import pytest

from phoson_llm.schemas import Message
from phoson_agent.sessions import JsonlStorage, ConversationTree


def _tree() -> ConversationTree:
    tree = ConversationTree.new()
    tree.append(None, Message(role="user", content="hi"))
    return tree


def _leaf(tree: ConversationTree) -> str:
    return max(tree.nodes.values(), key=lambda n: n.created_at).id


async def test_title_update_keeps_cost_and_token_totals(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree()
    await storage.save(tree)
    await storage.save_meta(
        tree.session_id,
        {
            "total_cost_usd": 1.5,
            "total_input_tokens": 100,
            "total_output_tokens": 20,
            "step_count": 3,
            "last_model": "m",
        },
    )

    await storage.save_meta(tree.session_id, {"title": "Named"})

    loaded = await storage.load(tree.session_id)
    assert loaded.title == "Named"
    assert loaded.total_cost == 1.5
    assert loaded.total_input_tokens == 100
    assert loaded.total_output_tokens == 20
    assert loaded.total_tokens == 120
    assert loaded.step_count == 3
    assert loaded.last_model == "m"


async def test_concurrent_saves_do_not_fail_or_leave_temp_files(
    tmp_path: Path,
) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree()

    results = await asyncio.gather(
        *[storage.save(tree) for _ in range(50)], return_exceptions=True
    )

    assert [r for r in results if isinstance(r, BaseException)] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{tree.session_id}.jsonl"]


async def test_meta_update_interleaved_with_saves_keeps_latest_nodes(
    tmp_path: Path,
) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree()
    await storage.save(tree)

    tree.append(_leaf(tree), Message(role="assistant", content="hello"))
    await asyncio.gather(
        storage.save_meta(tree.session_id, {"title": "T"}),
        storage.save(tree),
    )
    await storage.save_meta(tree.session_id, {"title": "T2"})

    loaded = await storage.load(tree.session_id)
    assert len(loaded.nodes) == len(tree.nodes)
    assert loaded.title == "T2"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
async def test_session_files_are_private(tmp_path: Path) -> None:
    base = tmp_path / "sessions"
    storage = JsonlStorage(base)
    tree = _tree()
    await storage.save(tree)

    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    file_mode = stat.S_IMODE((base / f"{tree.session_id}.jsonl").stat().st_mode)
    assert file_mode == 0o600


async def test_malformed_line_is_skipped_on_load(tmp_path: Path) -> None:
    storage = JsonlStorage(tmp_path)
    tree = _tree()
    await storage.save(tree)
    path = tmp_path / f"{tree.session_id}.jsonl"
    path.write_text(path.read_text() + '{"truncated": \n', encoding="utf-8")

    loaded = await storage.load(tree.session_id)

    assert len(loaded.nodes) == len(tree.nodes)
