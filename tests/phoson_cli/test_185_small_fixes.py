"""Tests for the small, independent CLI fixes grouped in issue #185.

Covers F-34 (``/resume`` token mapping), F-35 (``/compact`` persistence +
header refresh, ``/new``/``/resume`` ``is_running`` guard), F-36
(``_resolve_bool`` / ``_resolve_int`` / ``save_config``), F-37 (``/mcp config``
``expanduser`` + ``mcps.json`` perms) and F-38 (updater timeout).
"""

import os
import json
import stat as statmod
import asyncio
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import (
    PhosonConfig,
    PhosonConfigError,
    _parse_bool,
    save_config,
    _resolve_int,
    _resolve_bool,
    _parse_bool_file,
)
from phoson_cli.updater import run_upgrade_command
from phoson_agent.sessions.models import ConversationTree
from phoson_agent.sessions.serialization import apply_tree_meta, tree_meta_to_dict
from phoson_agent.sessions.storage_jsonl import JsonlStorage

# ── F-36: _resolve_bool accepts true/false/1/0/yes/no (case-insensitive) ─────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("False", False),
        ("0", False),
        ("1", True),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
    ],
)
def test_resolve_bool_accepts_common_spellings(value, expected) -> None:
    fd = {"show_reasoning": value}
    with patch.dict(os.environ, {}, clear=True):
        assert (
            _resolve_bool("PHOSON_SHOW_REASONING", "show_reasoning", fd, True)
            is expected
        )


def test_resolve_bool_rejects_garbage_with_config_error() -> None:
    with pytest.raises(PhosonConfigError, match="show_reasoning"):
        _parse_bool_file("maybe", True, "show_reasoning")


def test_parse_bool_env_accepts_falsy_spellings() -> None:
    # The env path uses _parse_bool (warns on garbage) — 'false' must be False.
    assert _parse_bool("false", True) is False
    assert _parse_bool("no", True) is False
    assert _parse_bool("0", True) is False
    assert _parse_bool("true", False) is True


def test_resolve_int_rejects_garbage_with_config_error() -> None:
    fd = {"max_iterations": "lots"}
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(PhosonConfigError, match="max_iterations"):
            _resolve_int("PHOSON_MAX_ITERATIONS", "max_iterations", fd, 50)


def test_resolve_int_parses_numeric_string() -> None:
    fd = {"max_iterations": "120"}
    with patch.dict(os.environ, {}, clear=True):
        assert _resolve_int("PHOSON_MAX_ITERATIONS", "max_iterations", fd, 50) == 120


def test_save_config_does_not_persist_env_only_secret(tmp_path, monkeypatch) -> None:
    # F-36: a secret that only exists in the process env must not be written
    # to disk by a bare full save.
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = PhosonConfig(openai_api_key="sk-env-only")
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env-only"}, clear=False):
        save_config(cfg)
    text = (home / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert "openai_api_key" not in text


def test_save_config_persisted_file_secret_is_kept(tmp_path, monkeypatch) -> None:
    # A secret already in the file is legitimately re-written.
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.toml"
    config_file.write_text(
        '[defaults]\nopenai_api_key = "sk-in-file"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    cfg = PhosonConfig(openai_api_key="sk-in-file")
    with patch.dict(os.environ, {}, clear=False):  # no env var
        save_config(cfg)
    assert 'openai_api_key = "sk-in-file"' in config_file.read_text()


# ── F-37: /mcp config expanduser + mcps.json perms ──────────────────────────


def test_mcp_config_set_expands_user(tmp_path, monkeypatch) -> None:
    from phoson_cli._mcp_commands import _MCPSubcommands

    home = tmp_path / "home"
    (home / "phoson").mkdir(parents=True)
    target = home / "phoson" / "mcps.json"
    monkeypatch.setenv("HOME", str(home))

    parent = SimpleNamespace(
        repl=SimpleNamespace(
            config=SimpleNamespace(
                mcp_config_file=Path("~/.phoson/mcps.json").expanduser(),
                enable_mcp=False,
            ),
        ),
        host=SimpleNamespace(
            print_info=MagicMock(), print_error=MagicMock(), print_warn=MagicMock()
        ),
    )
    handler = _MCPSubcommands.__new__(_MCPSubcommands)
    handler._parent = parent

    with patch("phoson_cli._mcp_commands.save_config"):
        asyncio.run(handler._set_config("~/phoson/mcps.json"))

    assert str(parent.repl.config.mcp_config_file) == str(target)


def test_toggle_mcp_config_restores_owner_only_perms(tmp_path) -> None:
    from phoson_cli._mcp_commands import toggle_mcp_config

    path = tmp_path / "mcps.json"
    path.write_text(
        json.dumps({"mcpServers": {"filesystem": {"command": "npx"}}}),
        encoding="utf-8",
    )
    os.chmod(path, 0o644)  # world-readable before

    toggle_mcp_config(path, "filesystem")
    assert statmod.S_IMODE(path.stat().st_mode) == 0o600  # F-37: owner-only


# ── F-38: updater timeout ───────────────────────────────────────────────────


def test_run_upgrade_command_times_out() -> None:
    # A subprocess that never finishes must be killed at the deadline and
    # reported as exit 124 (GNU timeout convention), not hang the REPL.
    async def go() -> tuple[int, str]:
        return await run_upgrade_command(["sleep", "10"], timeout=0.3)

    code, output = asyncio.run(go())
    assert code == 124
    assert "timed out" in output


def test_run_upgrade_command_normal() -> None:
    async def go() -> tuple[int, str]:
        return await run_upgrade_command(["true"], timeout=5.0)

    code, _ = asyncio.run(go())
    assert code == 0


# ── F-34: /resume maps the input/output split, not the sum → output ─────────


def test_tree_meta_round_trips_input_output_split() -> None:
    tree = ConversationTree.new("s1")
    tree.total_tokens = 500
    tree.total_input_tokens = 200
    tree.total_output_tokens = 300
    data = tree_meta_to_dict(tree)
    assert data["total_input_tokens"] == 200
    assert data["total_output_tokens"] == 300

    restored = ConversationTree.new("s1")
    apply_tree_meta(restored, data)
    assert restored.total_input_tokens == 200
    assert restored.total_output_tokens == 300


def test_tree_meta_legacy_sum_backfills_output() -> None:
    # A pre-F-34 record only has the total_tokens sum.
    data = {"type": "session_meta", "session_id": "s", "total_tokens": 500}
    tree = ConversationTree.new("s")
    apply_tree_meta(tree, data)
    assert tree.total_tokens == 500
    assert tree.total_input_tokens == 0
    assert tree.total_output_tokens == 500  # back-filled under output


async def test_save_meta_persists_split(tmp_path) -> None:
    storage = JsonlStorage(base_path=tmp_path)
    tree = ConversationTree.new("sid")
    tree.append(None, _text_msg("hi"))
    await storage.save(tree)
    await storage.save_meta(
        "sid",
        {
            "total_cost_usd": 0.0,
            "total_input_tokens": 200,
            "total_output_tokens": 300,
            "step_count": 4,
            "last_model": "m",
        },
    )
    metas = await storage.list_meta()
    assert metas[0].total_input_tokens == 200
    assert metas[0].total_output_tokens == 300
    assert metas[0].total_tokens == 500


async def test_load_session_maps_split_not_sum_to_output(tmp_path) -> None:
    """F-34: resuming a session maps the persisted input/output split."""
    from phoson_cli.controller import SessionController

    storage = JsonlStorage(base_path=tmp_path)
    tree = ConversationTree.new("sid123")
    for i in range(3):
        tree.append(None, _text_msg(f"m{i}"))
    await storage.save(tree)
    await storage.save_meta(
        "sid123",
        {
            "total_cost_usd": 0.5,
            "total_input_tokens": 200,
            "total_output_tokens": 300,
            "step_count": 4,
            "last_model": "m",
        },
    )

    config = PhosonConfig(provider="ollama", model="t", sessions_dir=tmp_path)
    chat = MagicMock(aclose=AsyncMock())
    with patch("phoson_cli.controller.build_chat", return_value=chat):
        controller = SessionController(config, FakeSink())

    outcome = await controller.load_session("sid123")
    assert outcome.ok
    assert controller.session_metrics.total_input_tokens == 200
    assert controller.session_metrics.total_output_tokens == 300
    # Not the sum dumped into output.
    assert controller.session_metrics.total_output_tokens != 500


# ── F-35: /compact persists + refreshes header ──────────────────────────────


async def test_manual_compact_persists_and_refreshes_header(tmp_path) -> None:
    from phoson_cli.controller import SessionController

    config = PhosonConfig(provider="ollama", model="t", sessions_dir=tmp_path)
    chat = MagicMock()
    chat.complete = AsyncMock(return_value=SimpleNamespace(content="SUMMARY"))
    chat.aclose = AsyncMock()
    with patch("phoson_cli.controller.build_chat", return_value=chat):
        controller = SessionController(config, FakeSink())

    last = None
    for i in range(12):
        node = controller.tree.append(last, _text_msg(f"m{i}"))
        last = node.id
    controller.current_node_id = last

    controller._context_tokens = 12345  # stale header value
    controller._save_session = AsyncMock()
    controller._profile_keep = lambda profile: 4

    before, after, changed = await controller.compact_context(None)
    assert changed is True
    assert controller._context_tokens != 12345  # header refreshed
    controller._save_session.assert_awaited_once()  # tree persisted


# ── F-35: /new and /resume guard is_running ─────────────────────────────────


async def test_cmd_new_rejected_while_running() -> None:
    from phoson_cli.commands import Command, CommandHandler

    repl = SimpleNamespace(is_running=True, new_session=MagicMock())
    host = SimpleNamespace(
        print_info=MagicMock(), print_warn=MagicMock(), print_error=MagicMock()
    )
    handler = CommandHandler.__new__(CommandHandler)
    handler.repl = repl
    handler.host = host

    await handler._cmd_new(Command(name="/new", args=""))
    repl.new_session.assert_not_called()
    host.print_warn.assert_called()


async def test_cmd_resume_rejected_while_running() -> None:
    from phoson_cli.commands import Command, CommandHandler

    repl = SimpleNamespace(
        is_running=True,
        storage=SimpleNamespace(list_meta=AsyncMock(return_value=[])),
        load_session=AsyncMock(return_value=True),
    )
    host = SimpleNamespace(
        print_info=MagicMock(), print_warn=MagicMock(), print_error=MagicMock()
    )
    handler = CommandHandler.__new__(CommandHandler)
    handler.repl = repl
    handler.host = host

    await handler._cmd_resume(Command(name="/resume", args="abc"))
    repl.load_session.assert_not_awaited()
    host.print_warn.assert_called()


# ── helpers ─────────────────────────────────────────────────────────────────


def _text_msg(content: str):
    from phoson_llm.schemas import Message

    return Message(role="user", content=content)


class FakeSink:
    def on_user_message(self, text, message):
        pass

    def on_attachments(self, sources):
        pass

    def on_event(self, event):
        pass

    def flush_line(self):
        pass

    def capture_partial_reasoning(self):
        pass

    def take_reasoning(self):
        return ""

    def set_session(self, session_id):
        pass

    def print_history(self, path, tail=None):
        pass

    def notify(self, kind, message):
        pass

    def on_subagent_progress(self, progress):
        pass
