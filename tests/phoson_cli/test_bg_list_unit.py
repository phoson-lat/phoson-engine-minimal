"""Tests for #129 Slice 2: ``phoson-cli bg list``.

``bg list`` is a pure read over the local session files — no daemon, no
network, no writes. These tests cover the table rendering (``list_bg_sessions``)
and the argv routing (``parse_args`` → ``bg_args``), plus the end-to-end
``run_bg_command`` with a stubbed config.
"""

import os
import json
import time
import asyncio
from pathlib import Path
from unittest.mock import patch

from phoson_llm.schemas import Message
from phoson_cli.__main__ import parse_args
from phoson_cli.commands import run_bg_command, list_bg_sessions
from phoson_agent.sessions import JsonlStorage, ConversationTree

# ── parse_args: bg routing ────────────────────────────────────────────────────


def test_parse_args_bg_list():
    options = parse_args(["bg", "list"])
    assert options.bg_args == ["list"]
    assert options.task is None
    assert options.plugin_args is None


def test_parse_args_bg_no_subcommand_defaults_to_list():
    options = parse_args(["bg"])
    assert options.bg_args == []


def test_parse_args_bg_keeps_extra_args():
    options = parse_args(["bg", "show", "abc123"])
    assert options.bg_args == ["show", "abc123"]


def test_parse_args_bg_not_swallowed_as_task():
    """``bg`` must be a subcommand, never a one-shot task word."""
    options = parse_args(["bg", "list"])
    assert options.task is None


def test_parse_args_plugin_still_works_alongside_bg():
    options = parse_args(["plugin", "list"])
    assert options.plugin_args == ["list"]
    assert options.bg_args is None


# ── list_bg_sessions: rendering ───────────────────────────────────────────────


def _write_session_file(
    tmp_path: Path,
    session_id: str,
    *,
    status: str = "completed",
    last_model: str = "qwen3-8b",
    step_count: int = 1,
    total_tokens: int = 100,
    total_cost: float = 0.01,
    title: str | None = None,
) -> None:
    """Write a minimal but realistic session file (meta + one node)."""
    meta = {
        "type": "session_meta",
        "session_id": session_id,
        "total_cost": total_cost,
        "total_tokens": total_tokens,
        "total_input_tokens": total_tokens // 2,
        "total_output_tokens": total_tokens - total_tokens // 2,
        "step_count": step_count,
        "last_model": last_model,
        "title": title,
        "status": status,
        "last_run_id": f"run-{session_id}",
    }
    node = {
        "id": "n1",
        "parent_id": None,
        "message": {"role": "user", "content": "hi"},
        "created_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    (tmp_path / f"{session_id}.jsonl").write_text(
        json.dumps(meta) + "\n" + json.dumps(node) + "\n", encoding="utf-8"
    )


def test_bg_list_missing_directory():
    missing = Path("/nonexistent/phoson/sessions")
    assert list_bg_sessions(missing) == "No sessions found."


def test_bg_list_empty_directory(tmp_path):
    assert list_bg_sessions(tmp_path) == "No sessions found."


def test_bg_list_renders_all_columns(tmp_path):
    _write_session_file(
        tmp_path,
        "a3f2b1c4",
        status="completed",
        last_model="qwen3-8b",
        step_count=12,
        total_tokens=45200,
        total_cost=0.02,
    )
    table = list_bg_sessions(tmp_path)
    lines = table.splitlines()
    assert len(lines) == 2  # header + one row
    header = lines[0]
    for col in ("ID", "STATUS", "MODEL", "STEPS", "TOKENS", "COST", "LAST ACTIVITY"):
        assert col in header
    row = lines[1]
    assert "a3f2b1c4" in row
    assert "completed" in row
    assert "qwen3-8b" in row
    assert "12" in row
    assert "45,200" in row
    assert "$0.02" in row


def test_bg_list_sorts_most_recent_first(tmp_path):
    _write_session_file(tmp_path, "old00001", total_cost=0.01)
    old_file = tmp_path / "old00001.jsonl"
    _write_session_file(tmp_path, "new00002", total_cost=0.02)
    new_file = tmp_path / "new00002.jsonl"

    now = time.time()
    os.utime(old_file, (now - 3600, now - 3600))
    os.utime(new_file, (now, now))

    table = list_bg_sessions(tmp_path)
    lines = table.splitlines()
    # Header first, then the newer session before the older one.
    assert lines[1].startswith("new00002")
    assert lines[2].startswith("old00001")


def test_bg_list_shows_orphaned_status(tmp_path):
    _write_session_file(tmp_path, "b7c1d2e3", status="orphaned")
    table = list_bg_sessions(tmp_path)
    assert "orphaned" in table


def test_bg_list_shows_active_status(tmp_path):
    _write_session_file(tmp_path, "c9d0e1f2", status="active")
    table = list_bg_sessions(tmp_path)
    assert "active" in table


def test_bg_list_cost_under_a_cent_rounds_to_zero(tmp_path):
    _write_session_file(tmp_path, "tiny0001", total_cost=0.001)
    table = list_bg_sessions(tmp_path)
    assert "$0.00" in table


def test_bg_list_no_model_shows_dash(tmp_path):
    _write_session_file(tmp_path, "nomod001", last_model=None)
    table = list_bg_sessions(tmp_path)
    lines = table.splitlines()
    # Columns are fixed-width; the MODEL column is the 3rd.
    assert lines[1].split()[2] == "-"


def test_bg_list_ignores_non_jsonl_files(tmp_path):
    _write_session_file(tmp_path, "real0001")
    (tmp_path / "notes.txt").write_text("not a session", encoding="utf-8")
    table = list_bg_sessions(tmp_path)
    lines = table.splitlines()
    assert len(lines) == 2
    assert "real0001" in lines[1]


def test_bg_list_is_read_only(tmp_path):
    """``bg list`` must not create or modify anything in the sessions dir."""
    _write_session_file(tmp_path, "ro000001")
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    list_bg_sessions(tmp_path)
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    assert before == after


def test_bg_list_with_real_storage_roundtrip(tmp_path):
    """A session written by the real JsonlStorage lists with its status."""
    tree = ConversationTree.new(session_id="stor0001")
    tree.append(parent_id=None, message=Message(role="user", content="hi"))
    tree.status = "aborted"
    tree.last_run_id = "run-stor"
    tree.update_session_meta(
        total_cost=1.5,
        total_tokens=300,
        step_count=3,
        last_model="claude-3-haiku",
    )
    asyncio.run(JsonlStorage(base_path=tmp_path).save(tree))

    table = list_bg_sessions(tmp_path)
    assert "stor0001" in table
    assert "aborted" in table
    assert "claude-3-haiku" in table
    assert "$1.50" in table


# ── run_bg_command: dispatch ──────────────────────────────────────────────────


def _fake_config(tmp_path: Path):
    return type("FakeConfig", (), {"sessions_dir": tmp_path})()


def test_run_bg_command_list_prints_table(tmp_path, capsys):
    _write_session_file(tmp_path, "cmd00001", status="completed")
    with patch("phoson_cli.config.load_config", return_value=_fake_config(tmp_path)):
        code = run_bg_command(["list"])
    captured = capsys.readouterr()
    assert code == 0
    assert "cmd00001" in captured.out
    assert "STATUS" in captured.out


def test_run_bg_command_no_args_defaults_to_list(tmp_path, capsys):
    _write_session_file(tmp_path, "cmd00002", status="active")
    with patch("phoson_cli.config.load_config", return_value=_fake_config(tmp_path)):
        code = run_bg_command([])
    captured = capsys.readouterr()
    assert code == 0
    assert "cmd00002" in captured.out


def test_run_bg_command_unknown_subcommand_exits_2(capsys):
    code = run_bg_command(["frobnicate"])
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown bg command" in captured.err


def test_run_bg_command_falls_back_when_config_is_broken(tmp_path, capsys):
    """`bg list` is a diagnostic — it must still work when the config is
    unreadable, falling back to the default sessions dir."""
    _write_session_file(tmp_path, "fb000001", status="active")

    def _broken_config():
        raise RuntimeError("config.toml is on fire")

    with (
        patch("phoson_cli.config.load_config", side_effect=_broken_config),
        patch(
            "phoson_cli.config.PhosonConfig",
            return_value=_fake_config(tmp_path),
        ),
    ):
        code = run_bg_command(["list"])
    captured = capsys.readouterr()
    assert code == 0
    assert "fb000001" in captured.out
