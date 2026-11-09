"""Teams contract: per-session identity and the peers bus between sidecars."""

import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoson_cli.config import PhosonConfig
from phoson_bridge.server import Bridge, SessionManager, _peer_identity
from phoson_plugin_peers.storage import PeerError, PeerStore


async def test_named_session_config_does_not_touch_base() -> None:
    base = PhosonConfig(
        provider="ollama", model="test", sessions_dir=Path(tempfile.mkdtemp())
    )
    manager = SessionManager(base, lambda *_args, **_kwargs: None)
    key = manager.create("backend", "platform")
    named = manager.config_for(key)
    assert named.peer_name == "backend"
    assert named.peer_team == "platform"
    assert base.peer_name == ""
    await manager.close(key)


def test_peer_identity_is_normalized() -> None:
    assert _peer_identity(
        {"peerName": " Backend-Agent ", "peerTeam": " Platform "}
    ) == (
        "backend-agent",
        "platform",
    )
    assert _peer_identity({}) == ("", "default")


async def test_send_queues_offline_and_lists_presence(tmp_path: Path) -> None:
    bridge = SimpleNamespace(config=SimpleNamespace(peers_data_dir=tmp_path))
    sent = await Bridge._peers_send(
        bridge,
        {
            "team": "platform",
            "sender": "backend",
            "recipient": "frontend",
            "content": "Revisa la UI",
        },
    )
    assert sent["ok"] is True
    assert sent["delivered"] is False
    unread = PeerStore(tmp_path, "platform").unread("frontend")
    assert [message.content for _path, message in unread] == ["Revisa la UI"]

    store = PeerStore(tmp_path, "platform")
    store.claim("frontend", instance="test", cwd="/workspace/web")
    sent = await Bridge._peers_send(
        bridge,
        {
            "team": "platform",
            "sender": "backend",
            "recipient": "frontend",
            "content": "¿Estás?",
        },
    )
    assert sent["delivered"] is True
    listed = await Bridge._peers_list(bridge, {"team": "platform"})
    assert listed["peers"][0]["name"] == "frontend"
    assert listed["peers"][0]["live"] is True
    assert listed["peers"][0]["cwd"] == "/workspace/web"


async def test_send_rejects_self_message(tmp_path: Path) -> None:
    bridge = SimpleNamespace(config=SimpleNamespace(peers_data_dir=tmp_path))
    with pytest.raises(PeerError):
        await Bridge._peers_send(
            bridge,
            {
                "team": "default",
                "sender": "same",
                "recipient": "same",
                "content": "loop",
            },
        )
