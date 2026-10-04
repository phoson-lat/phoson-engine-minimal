"""Unit tests for the named-peers plugin: storage, tools, wakes, auto-reply.

Two plugin instances in one process stand in for two CLI windows; they share
a temp ``data_dir``. A small fake "host" drives the wake/turn hooks the same
way ``SessionController`` does (drain → run turn → on_turn_end).
"""

import os
import json
import time
import asyncio
from types import SimpleNamespace
from typing import Any
from pathlib import Path

import pytest

from phoson_agent import CliCommandInvocation
from phoson_plugin_peers import PeerError, PeersPlugin, render_wake_message
from phoson_plugin_peers.storage import (
    KIND_REPLY,
    KIND_REQUEST,
    PeerStore,
    PeerMessage,
    normalize_name,
)


def _make(tmp_path: Path, name: str, **config: Any) -> PeersPlugin:
    plugin = PeersPlugin()
    plugin.configure({"name": name, "data_dir": str(tmp_path), **config})
    plugin.initialize()
    return plugin


def _tools(plugin: PeersPlugin) -> dict[str, Any]:
    return {t.name: t for t in plugin.get_tools()}


async def _run_host_turn(plugin: PeersPlugin, answer: str, status: str = "done"):
    """What the CLI does on a wake: drain, run the turn, report the outcome."""
    drained = plugin.drain_pending_wakes(None)
    plugin.on_turn_end(SimpleNamespace(status=status, final_content=answer))
    return drained


async def _wait_for_wake(plugin: PeersPlugin, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not plugin.pending_wakes(None):
        if time.monotonic() > deadline:
            raise AssertionError("no wake arrived")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    import phoson_plugin_peers._plugin as mod

    monkeypatch.setattr(mod, "_ASK_POLL_SECONDS", 0.01)


# ── Names & presence ────────────────────────────────────────────────────────


class TestNames:
    def test_normalize_lowercases(self) -> None:
        assert normalize_name("Backend-Agent") == "backend-agent"

    @pytest.mark.parametrize("bad", ["", " ", "-x", "a/b", "x" * 65, "a b"])
    def test_rejects_bad_names(self, bad: str) -> None:
        with pytest.raises(PeerError):
            normalize_name(bad)

    def test_initialize_requires_name(self, tmp_path: Path) -> None:
        plugin = PeersPlugin()
        plugin.configure({"data_dir": str(tmp_path)})
        with pytest.raises(PeerError, match="name"):
            plugin.initialize()


class TestPresence:
    def test_claim_writes_presence(self, tmp_path: Path) -> None:
        plugin = _make(tmp_path, "backend-agent")
        store = PeerStore(tmp_path, "default")
        live = store.live("backend-agent")
        assert live is not None and live.pid == os.getpid()
        plugin.cleanup()
        assert store.get("backend-agent") is None

    def test_name_taken_by_live_other_process(self, tmp_path: Path) -> None:
        store = PeerStore(tmp_path, "default")
        store.claim("backend-agent", instance="x", cwd="/elsewhere")
        # Pretend another live process owns it (pid 1 always exists).
        path = store.root / "agents" / "backend-agent.json"
        raw = json.loads(path.read_text())
        raw["pid"] = 1
        path.write_text(json.dumps(raw))
        with pytest.raises(PeerError, match="already in use"):
            _make(tmp_path, "backend-agent")

    def test_stale_name_is_taken_over(self, tmp_path: Path) -> None:
        store = PeerStore(tmp_path, "default")
        store.claim("backend-agent", instance="old", cwd="/old")
        path = store.root / "agents" / "backend-agent.json"
        raw = json.loads(path.read_text())
        raw.update(pid=1, heartbeat=time.time() - 3600)
        path.write_text(json.dumps(raw))
        plugin = _make(tmp_path, "backend-agent")
        assert store.get("backend-agent").instance == plugin._instance

    def test_rebuild_cleanup_does_not_drop_successor(self, tmp_path: Path) -> None:
        old = _make(tmp_path, "backend-agent")
        new = _make(tmp_path, "backend-agent")  # engine rebuild, same pid
        old.cleanup()
        store = PeerStore(tmp_path, "default")
        assert store.live("backend-agent").instance == new._instance

    def test_teams_are_isolated(self, tmp_path: Path) -> None:
        _make(tmp_path, "a", team="one")
        b = _make(tmp_path, "b", team="two")
        assert "No other agent" in asyncio.run(_tools(b)["peer_list"].handler({}, {}))


# ── Tools ───────────────────────────────────────────────────────────────────


class TestTools:
    def test_schemas(self, tmp_path: Path) -> None:
        tools = _tools(_make(tmp_path, "a"))
        assert list(tools) == ["peer_list", "peer_send", "peer_ask"]
        assert tools["peer_ask"].parameters["required"] == ["to", "content"]
        assert "'a'" in tools["peer_list"].description

    async def test_peer_list_shows_others(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "frontend-agent")
        _make(tmp_path, "backend-agent")
        out = await _tools(a)["peer_list"].handler({}, {})
        assert "backend-agent: idle" in out
        assert "frontend-agent:" not in out

    async def test_send_unknown_and_self(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a")
        out = await _tools(a)["peer_send"].handler({"to": "ghost", "content": "x"}, {})
        assert "no agent named 'ghost'" in out
        out = await _tools(a)["peer_send"].handler({"to": "a", "content": "x"}, {})
        assert "yourself" in out

    async def test_send_wakes_recipient(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "frontend-agent")
        b = _make(tmp_path, "backend-agent")
        out = await _tools(a)["peer_send"].handler(
            {"to": "backend-agent", "content": "deploy done"}, {}
        )
        assert "Delivered" in out
        pending = b.pending_wakes(None)
        assert [m.content for m in pending] == ["deploy done"]
        drained = b.drain_pending_wakes(None)
        assert len(drained) == 1 and b.pending_wakes(None) == []
        # Read receipt: the message moved new/ -> cur/.
        assert PeerStore(tmp_path, "default").is_read("backend-agent", drained[0].id)

    async def test_send_to_offline_is_queued(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a")
        b = _make(tmp_path, "b")
        b.cleanup()
        PeerStore(tmp_path, "default").claim("b", instance="z")  # file exists...
        path = tmp_path / "default" / "agents" / "b.json"
        raw = json.loads(path.read_text())
        raw["heartbeat"] = 0  # ...but stale
        path.write_text(json.dumps(raw))
        out = await _tools(a)["peer_send"].handler({"to": "b", "content": "hi"}, {})
        assert "offline" in out and "Queued" in out


# ── peer_ask round trip ─────────────────────────────────────────────────────


class TestAsk:
    async def test_round_trip_auto_reply(self, tmp_path: Path) -> None:
        front = _make(tmp_path, "frontend-agent")
        back = _make(tmp_path, "backend-agent")

        ask = asyncio.create_task(
            _tools(front)["peer_ask"].handler(
                {"to": "backend-agent", "content": "Pásame la doc de la API"}, {}
            )
        )
        await _wait_for_wake(back)
        # The requester is visible as waiting on the recipient.
        assert PeerStore(tmp_path, "default").get("frontend-agent").state == "waiting"
        drained = await _run_host_turn(back, "GET /users, POST /users")
        assert drained[0].kind == KIND_REQUEST
        assert drained[0].content == "Pásame la doc de la API"

        result = await asyncio.wait_for(ask, 5)
        assert result == "Reply from backend-agent:\nGET /users, POST /users"
        # The reply was consumed by the tool, not left as a wake.
        assert front.pending_wakes(None) == []

    async def test_failed_turn_replies_with_error(self, tmp_path: Path) -> None:
        front = _make(tmp_path, "a")
        back = _make(tmp_path, "b")
        ask = asyncio.create_task(
            _tools(front)["peer_ask"].handler({"to": "b", "content": "q"}, {})
        )
        await _wait_for_wake(back)
        await _run_host_turn(back, "", status="cancelled")
        out = await asyncio.wait_for(ask, 5)
        assert "could not finish the turn (cancelled)" in out

    async def test_ask_offline_fails_fast(self, tmp_path: Path) -> None:
        front = _make(tmp_path, "a")
        back = _make(tmp_path, "b")
        back.cleanup()
        PeerStore(tmp_path, "default").claim("b", instance="z")
        path = tmp_path / "default" / "agents" / "b.json"
        raw = json.loads(path.read_text())
        raw["heartbeat"] = 0
        path.write_text(json.dumps(raw))
        with pytest.raises(PeerError, match="offline"):
            await front.ask("b", "q")

    async def test_ask_timeout_reports_unread(self, tmp_path: Path) -> None:
        front = _make(tmp_path, "a")
        _make(tmp_path, "b")
        with pytest.raises(PeerError, match="has not read"):
            await front.ask("b", "q", timeout=0.1)

    async def test_deadlock_detected(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a")
        b = _make(tmp_path, "b")
        pending = asyncio.create_task(a.ask("b", "q", timeout=5))
        await _wait_for_wake(b)
        with pytest.raises(PeerError, match="deadlock"):
            await b.ask("a", "q2")
        await _run_host_turn(b, "ok")
        assert await asyncio.wait_for(pending, 5) == "ok"

    async def test_late_reply_becomes_wake(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a")
        b = _make(tmp_path, "b")
        with pytest.raises(PeerError, match="no reply"):
            await a.ask("b", "q", timeout=0.1)
        await _run_host_turn(b, "late answer")
        pending = a.pending_wakes(None)
        assert [m.kind for m in pending] == [KIND_REPLY]
        assert "late reply" in render_wake_message(pending, "a")


# ── Hops / loop guard ───────────────────────────────────────────────────────


class TestHops:
    async def test_hops_increment_and_cap(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a", max_hops=2)
        b = _make(tmp_path, "b", max_hops=2)
        send_a = _tools(a)["peer_send"].handler
        send_b = _tools(b)["peer_send"].handler

        await send_a({"to": "b", "content": "1"}, {})  # hops=1
        assert b.drain_pending_wakes(None)[0].hops == 1
        await send_b({"to": "a", "content": "2"}, {})  # hops=2 (inside b's turn)
        assert a.drain_pending_wakes(None)[0].hops == 2
        out = await send_a({"to": "b", "content": "3"}, {})  # would be 3 > 2
        assert "hop limit" in out

    async def test_user_turn_resets_hops(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a", max_hops=1)
        _make(tmp_path, "b", max_hops=1)
        a.drain_pending_wakes(None)  # user turn with no messages
        out = await _tools(a)["peer_send"].handler({"to": "b", "content": "x"}, {})
        assert "Delivered" in out


# ── Rendering & status ──────────────────────────────────────────────────────


class TestRendering:
    def test_wake_message_format(self) -> None:
        msg = PeerMessage(
            id="abc",
            sender="frontend-agent",
            recipient="b",
            content="hola",
            kind=KIND_REQUEST,
        )
        text = render_wake_message([msg], "backend-agent")
        assert text.startswith("[PEER MESSAGES]")
        assert '"backend-agent"' in text
        assert ">>> request from frontend-agent" in text and "\n<<<" in text
        assert "sent back automatically" in text

    def test_cli_renders_peer_card(self) -> None:
        from rich.console import Console

        from phoson_cli.theme import load_theme
        from phoson_cli.formatting import render_user_turn

        msg = PeerMessage(
            id="abc",
            sender="frontend-agent",
            recipient="b",
            content="Pásame la doc",
            kind=KIND_REQUEST,
        )
        console = Console(record=True, width=80)
        payload = render_wake_message([msg], "b")
        console.print(render_user_turn(payload, load_theme(None)))
        out = console.export_text()
        assert "📨 @frontend-agent asks you: Pásame la doc" in out
        assert "[PEER MESSAGES]" not in out

    def test_cli_peer_card_truncates_long_messages(self) -> None:
        from rich.console import Console

        from phoson_cli.theme import load_theme
        from phoson_cli.formatting import render_user_turn

        long_body = "Necesito la documentación completa de la API " * 20
        msg = PeerMessage(
            id="abc",
            sender="frontend-agent",
            recipient="b",
            content=long_body,
            kind=KIND_REQUEST,
        )
        console = Console(record=True, width=120)
        payload = render_wake_message([msg], "b")
        console.print(render_user_turn(payload, load_theme(None)))
        lines = [ln for ln in console.export_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("📨 @frontend-agent asks you: Necesito")
        assert "…" in lines[0]
        assert len(lines[0]) < 120

    def test_monitor_status(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a")
        assert a.monitor_status() == "👥 a"
        _make(tmp_path, "b")
        a._live_peers = a._count_live_peers()
        assert a.monitor_status() == "👥 a · 1 peer"


class TestCommands:
    async def test_tell(self, tmp_path: Path) -> None:
        a = _make(tmp_path, "a")
        b = _make(tmp_path, "b")
        notes: list[tuple[str, str]] = []
        ctx = SimpleNamespace(notify=lambda k, m: notes.append((k, m)))
        await a.handle_tell(CliCommandInvocation(name="/tell", args="b hola b"), ctx)
        assert [m.content for m in b.pending_wakes(None)] == ["hola b"]
        assert notes[-1][0] == "info"
        await a.handle_tell(CliCommandInvocation(name="/tell", args="b"), ctx)
        assert "Usage" in notes[-1][1]
