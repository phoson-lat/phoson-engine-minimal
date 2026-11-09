"""Remote-access gateway: pairing, auth, routing, mailboxes, restarts."""

import os
import sys
import json
import stat
import asyncio
from pathlib import Path

import pytest

from phoson_bridge import gateway as G
from phoson_bridge.devices import MAX_CODE_ATTEMPTS, DeviceStore

#: A stand-in bridge: answers every request with its method name, emits a
#: notification for "emit", and exits on "die".
FAKE_BRIDGE = """
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "die":
        sys.exit(3)
    if msg.get("method") == "emit":
        print(json.dumps({"jsonrpc": "2.0", "method": "agent.event",
                          "params": msg.get("params", {})}), flush=True)
    if "id" in msg:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"method": msg["method"]}}), flush=True)
"""


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DeviceStore:
    monkeypatch.setenv("PHOSON_HOME", str(tmp_path / ".phoson"))
    return DeviceStore()


# ── Devices ──────────────────────────────────────────────────────────────────


def test_pairing_code_is_single_use_and_tokens_are_hashed(store: DeviceStore) -> None:
    code, _ = store.create_pairing_code()
    assert len(code) == 9 and code[4] == "-"
    assert store.consume_pairing_code(code.lower().replace("-", " "))
    assert not store.consume_pairing_code(code)
    device, token = store.add_device("phone")
    assert store.verify(token).id == device.id  # type: ignore[union-attr]
    assert store.verify("x" + token) is None
    raw = store.devices_path.read_text()
    assert token not in raw  # only the hash is stored
    if os.name == "posix":
        assert stat.S_IMODE(store.devices_path.stat().st_mode) == 0o600
    assert store.revoke(device.id) and store.verify(token) is None


def test_wrong_guesses_burn_pending_codes(store: DeviceStore) -> None:
    code, _ = store.create_pairing_code()
    for _ in range(MAX_CODE_ATTEMPTS):
        assert not store.consume_pairing_code("AAAA-AAAA")
    assert not store.consume_pairing_code(code)


def test_non_loopback_needs_tls_or_explicit_plaintext(tmp_path: Path) -> None:
    with pytest.raises(G.ConfigError, match="without TLS"):
        G.check_config(G.GatewayConfig(host="0.0.0.0", default_workspace=tmp_path))
    G.check_config(
        G.GatewayConfig(
            host="0.0.0.0", default_workspace=tmp_path, allow_plaintext=True
        )
    )
    with pytest.raises(G.ConfigError, match="go together"):
        G.check_config(
            G.GatewayConfig(default_workspace=tmp_path, tls_cert=tmp_path / "c.pem")
        )


# ── Gateway over in-memory connections ───────────────────────────────────────


class Conn:
    """An in-memory connection driving ``Gateway.handle``."""

    def __init__(self, gateway: G.Gateway, address: str = "10.0.0.1") -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.outbox: asyncio.Queue[str] = asyncio.Queue()
        self.task = asyncio.create_task(
            gateway.handle(self.inbox.get, self._send, address)
        )

    async def _send(self, line: str) -> None:
        await self.outbox.put(line)

    async def request(self, rid: int, method: str, **params) -> dict:
        await self.inbox.put(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            )
        )
        return await self.wait(lambda m: m.get("id") == rid and "method" not in m)

    async def notify(self, method: str, **params) -> None:
        await self.inbox.put(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        )

    async def wait(self, predicate) -> dict:
        while True:
            message = json.loads(await asyncio.wait_for(self.outbox.get(), 10))
            if predicate(message):
                return message

    async def close(self) -> None:
        await self.inbox.put(None)
        await asyncio.wait_for(self.task, 10)


@pytest.fixture
async def gateway(store: DeviceStore, tmp_path: Path):
    fake = tmp_path / "fake_bridge.py"
    fake.write_text(FAKE_BRIDGE)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = G.GatewayConfig(
        default_workspace=workspace, command=[sys.executable, str(fake)]
    )
    gw = G.Gateway(config, store)
    yield gw
    await gw.close()


async def _authed(gw: G.Gateway, store: DeviceStore, name: str) -> Conn:
    _device, token = store.add_device(name)
    conn = Conn(gw)
    assert "result" in await conn.request(1, "gateway.auth", token=token)
    return conn


async def test_handshake_routing_and_broadcast(gateway: G.Gateway, store) -> None:
    code, _ = store.create_pairing_code()
    pairing = Conn(gateway)
    paired = await pairing.request(1, "gateway.pair", code=code, deviceName="phone")
    assert paired["result"]["token"]
    assert (await pairing.request(2, "session.list"))["error"]["code"] == -32002
    await pairing.close()

    a = await _authed(gateway, store, "a")
    b = await _authed(gateway, store, "b")
    opened = await a.request(2, "gateway.open")
    assert opened["result"]["protocol"] == G.PROTOCOL_VERSION
    assert (await a.request(3, "gateway.open"))["error"]["code"] == -32002
    await b.request(2, "gateway.open")

    # Both clients use id 7; each gets only its own response, with its id.
    ra, rb = await asyncio.gather(a.request(7, "x.one"), b.request(7, "x.two"))
    assert ra["result"] == {"method": "x.one"} and rb["result"] == {"method": "x.two"}

    # Notifications reach every attached client.
    await a.notify("emit", n=1)
    for conn in (a, b):
        event = await conn.wait(lambda m: m.get("method") == "agent.event")
        assert event["params"] == {"n": 1}
    await a.close()
    await b.close()


async def test_failed_auth_is_rate_limited(gateway: G.Gateway, store) -> None:
    for _ in range(G.MAX_FAILURES):
        conn = Conn(gateway, "10.9.9.9")
        assert "error" in await conn.request(1, "gateway.auth", token="bad")
        await asyncio.wait_for(conn.task, 10)
    _device, token = store.add_device("late")
    blocked = Conn(gateway, "10.9.9.9")
    message = json.loads(await asyncio.wait_for(blocked.outbox.get(), 10))
    assert "too many" in message["error"]["message"]
    # Another address is unaffected.
    other = Conn(gateway, "10.0.0.2")
    assert "result" in await other.request(1, "gateway.auth", token=token)
    await other.close()


async def test_mailbox_replays_missed_notifications(gateway: G.Gateway, store) -> None:
    _device, phone_token = store.add_device("phone")
    phone = Conn(gateway)
    await phone.request(1, "gateway.auth", token=phone_token)
    await phone.request(2, "gateway.open")
    await phone.close()  # the phone drops off the network

    desktop = await _authed(gateway, store, "desktop")
    await desktop.request(2, "gateway.open")
    await desktop.notify("emit", turn="while away")
    await desktop.wait(lambda m: m.get("method") == "agent.event")

    back = Conn(gateway)
    await back.request(1, "gateway.auth", token=phone_token)
    reopened = await back.request(2, "gateway.open", resume=True)
    assert reopened["result"]["resumed"] is True
    missed = await back.wait(lambda m: m.get("method") == "agent.event")
    assert missed["params"] == {"turn": "while away"}
    await back.close()
    await desktop.close()


async def test_bridge_exit_is_reported_and_restarted(gateway: G.Gateway, store) -> None:
    conn = await _authed(gateway, store, "a")
    await conn.request(2, "gateway.open")
    await conn.inbox.put(json.dumps({"jsonrpc": "2.0", "method": "die"}))
    exited = await conn.wait(lambda m: m.get("method") == "gateway.bridgeExited")
    assert exited["params"]["code"] == 3
    reply = await conn.request(3, "x.again")  # restarts the bridge on demand
    assert reply["result"] == {"method": "x.again"}
    await conn.close()


async def test_websocket_server_refuses_browsers_and_other_paths(
    store, tmp_path: Path
) -> None:
    pytest.importorskip("websockets")
    from websockets.exceptions import InvalidStatus
    from websockets.asyncio.client import connect

    ports: list[int] = []
    config = G.GatewayConfig(port=0, default_workspace=tmp_path)
    server = asyncio.create_task(G.serve(config, ready=ports.append))
    while not ports:
        await asyncio.sleep(0.02)
    base = f"ws://127.0.0.1:{ports[0]}"
    try:
        info = json.loads(G.server_info_path().read_text())
        assert info["url"] == f"{base}/v1"
        for url, headers, status in (
            (f"{base}/v1", {"Origin": "https://evil.example"}, 403),
            (f"{base}/other", {}, 404),
        ):
            with pytest.raises(InvalidStatus) as caught:
                async with connect(url, additional_headers=headers):
                    pass
            assert caught.value.response.status_code == status
        async with connect(f"{base}/v1") as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "nope"}))
            assert json.loads(await ws.recv())["error"]["code"] == -32001
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
    assert not G.server_info_path().exists()


def test_remote_cli_pairs_lists_and_revokes(
    store: DeviceStore, capsys: pytest.CaptureFixture[str]
) -> None:
    from phoson_bridge.cli import run_serve, run_remote
    from phoson_cli.__main__ import parse_args

    assert parse_args(["serve", "--port", "0"]).serve_args == ["--port", "0"]
    assert parse_args(["remote", "pair"]).remote_args == ["pair"]
    assert parse_args(["bridge"]).bridge is True

    assert run_remote(["pair"]) == 0
    out = capsys.readouterr().out
    code = out.split("Pairing code: ")[1].split()[0]
    assert "phoson://pair?code=" in out
    assert store.consume_pairing_code(code)

    device, _token = store.add_device("tablet")
    assert run_remote(["devices"]) == 0
    assert "tablet" in capsys.readouterr().out
    assert run_remote(["revoke", device.id]) == 0
    assert run_remote(["revoke", device.id]) == 1
    assert run_serve(["--help"]) == 0
    assert "--allow-plaintext" in capsys.readouterr().out
