"""Remote-access gateway: the bridge protocol over WebSocket (``phoson-cli serve``).

Phoson Desktop runs one ``phoson-bridge`` process per workspace and talks
JSON-RPC to it, one JSON object per line, over stdin/stdout. The gateway
offers the same protocol to clients on other machines (Desktop in remote mode,
the mobile app) without changing the bridge:

- it starts one ``phoson-cli bridge`` child per workspace (``cwd`` = the
  workspace), exactly like Desktop does locally, and forwards lines both ways;
- a WebSocket text frame carries one protocol line, unchanged;
- several clients may attach to the same workspace at once: notifications
  (agent events, confirmations, metrics) go to all of them, and request ids
  are rewritten per client so every response reaches only the client that
  asked;
- a client that drops (a phone changing networks) keeps a **mailbox** of the
  notifications it missed for :data:`MAILBOX_TTL_S`; reconnecting with
  ``resume: true`` replays them, otherwise the client re-opens its sessions.

Connection handshake (JSON-RPC requests handled by the gateway itself):

1. ``gateway.pair {code, deviceName}`` → ``{deviceId, token}`` (first time), or
   ``gateway.auth {token}`` → ``{deviceId, name}``. Nothing else is accepted
   before this, and a connection must authenticate within
   :data:`AUTH_TIMEOUT_S`.
2. ``gateway.open {workspace?, resume?}`` → ``{workspace, protocol,
   serverVersion, resumed}``. One workspace per connection; open one
   connection per workspace, as Desktop opens one process per workspace.
3. Any other method goes to the workspace's bridge. ``gateway.info`` works at
   any time after step 1.

Security: a paired device has the same power as the local user (the bridge
runs commands and edits files). The gateway listens on loopback by default;
another address needs TLS (``--tls-cert/--tls-key``) or an explicit
``--allow-plaintext`` (for a private network such as Tailscale, which already
encrypts). Browser connections (an ``Origin`` header) are refused unless the
origin is allowed, so a web page cannot talk to a local gateway. Failed
authentication is rate limited per address.
"""

import os
import ssl
import sys
import json
import time
import signal
import socket
import asyncio
import hashlib
import logging
import itertools
from typing import Any
from pathlib import Path
from collections import deque
from dataclasses import field, dataclass

from .devices import DeviceStore, remote_dir

log = logging.getLogger("phoson_bridge.gateway")

PROTOCOL_VERSION = 1
DEFAULT_PORT = 7337
AUTH_TIMEOUT_S = 10.0
MAILBOX_TTL_S = 15 * 60.0
MAILBOX_MAX_LINES = 5000
MAX_FRAME_BYTES = 64 * 1024 * 1024
#: Failed auth/pair attempts allowed per address within the window.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 10 * 60.0

_ERR_AUTH = -32001
_ERR_STATE = -32002
_ERR_WORKSPACE = -32003
_ERR_BRIDGE = -32004


def _engine_version() -> str:
    try:
        from phoson_cli.updater import get_current_version

        return str(get_current_version())
    except Exception:  # noqa: BLE001 - informational only
        return "unknown"


def bridge_command() -> list[str]:
    """How to start a bridge child with this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "bridge"]
    return [sys.executable, "-m", "phoson_bridge"]


def _error(request_id: Any, code: int, message: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _result(request_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def _notification(method: str, params: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})


# ── Clients and mailboxes ────────────────────────────────────────────────────


class Client:
    """One authenticated WebSocket connection (transport-agnostic for tests)."""

    def __init__(self, send: Any, device_id: str, address: str) -> None:
        self._send = send
        self.device_id = device_id
        self.address = address
        self.closed = False

    async def send(self, line: str) -> None:
        if self.closed:
            return
        try:
            await self._send(line)
        except Exception:  # noqa: BLE001 - a dead socket is handled by its reader
            self.closed = True


@dataclass
class Mailbox:
    """Notifications a detached device missed."""

    detached_at: float
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAILBOX_MAX_LINES))
    overflowed: bool = False

    def add(self, line: str) -> None:
        if len(self.lines) == self.lines.maxlen:
            self.overflowed = True
        self.lines.append(line)

    def expired(self, now: float) -> bool:
        return now - self.detached_at > MAILBOX_TTL_S


# ── One bridge child per workspace ───────────────────────────────────────────


class WorkspaceHost:
    """A ``phoson-cli bridge`` child for one workspace and its clients."""

    def __init__(self, path: Path, command: list[str], log_dir: Path) -> None:
        self.path = path
        self.command = command
        self.log_dir = log_dir
        self.clients: set[Client] = set()
        self.mailboxes: dict[str, Mailbox] = {}
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[Client, Any]] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.restarts = 0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def ensure_running(self) -> None:
        async with self._lock:
            if self.running:
                return
            self.log_dir.mkdir(parents=True, exist_ok=True)
            tag = hashlib.sha256(str(self.path).encode()).hexdigest()[:10]
            errlog = open(self.log_dir / f"bridge-{tag}.log", "ab")  # noqa: SIM115
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.command,
                    cwd=str(self.path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=errlog,
                    limit=MAX_FRAME_BYTES,
                )
            finally:
                errlog.close()
            self._reader = asyncio.create_task(self._read_loop(self._proc))
            log.info("bridge started for %s (pid %s)", self.path, self._proc.pid)

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning("bridge line over %d bytes dropped", MAX_FRAME_BYTES)
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line:
                await self._route(line)
        code = await proc.wait()
        log.warning("bridge for %s exited with %s", self.path, code)
        await self._bridge_exited(code)

    async def _route(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        if "method" not in message and "id" in message:
            target = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
            if target is None:
                return
            client, original_id = target
            message["id"] = original_id
            await client.send(json.dumps(message))
            return
        for client in list(self.clients):
            await client.send(line)
        now = time.time()
        for device_id, box in list(self.mailboxes.items()):
            if box.expired(now):
                del self.mailboxes[device_id]
            else:
                box.add(line)

    async def _bridge_exited(self, code: int) -> None:
        for request_id, (client, original_id) in list(self._pending.items()):
            await client.send(_error(original_id, _ERR_BRIDGE, "bridge exited"))
            self._pending.pop(request_id, None)
        notice = _notification("gateway.bridgeExited", {"code": code})
        for client in list(self.clients):
            await client.send(notice)
        self.mailboxes.clear()

    async def forward(self, client: Client, message: dict[str, Any]) -> None:
        """Send a client's message to the bridge, rewriting its request id."""
        if not self.running:
            self.restarts += 1
            await self.ensure_running()
            await client.send(
                _notification("gateway.bridgeRestarted", {"restarts": self.restarts})
            )
        if "id" in message and message.get("id") is not None:
            internal = next(self._ids)
            self._pending[internal] = (client, message["id"])
            message = {**message, "id": internal}
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    def attach(self, client: Client, resume: bool) -> tuple[bool, list[str]]:
        """Add *client*; returns ``(resumed, missed lines)``."""
        for other in [c for c in self.clients if c.device_id == client.device_id]:
            # The same device reconnected: the old socket is stale.
            other.closed = True
            self.clients.discard(other)
        box = self.mailboxes.pop(client.device_id, None)
        self.clients.add(client)
        if not resume or box is None or box.overflowed or box.expired(time.time()):
            return False, []
        return True, list(box.lines)

    def detach(self, client: Client) -> None:
        if client not in self.clients:
            return
        self.clients.discard(client)
        for request_id, (owner, _orig) in list(self._pending.items()):
            if owner is client:
                self._pending.pop(request_id, None)
        if not any(c.device_id == client.device_id for c in self.clients):
            self.mailboxes[client.device_id] = Mailbox(detached_at=time.time())

    async def close(self, timeout: float = 5.0) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # EOF: the bridge shuts its sessions down
            await asyncio.wait_for(proc.wait(), timeout)
        except (TimeoutError, ProcessLookupError):
            proc.kill()
            await proc.wait()
        if self._reader is not None:
            self._reader.cancel()


# ── The gateway ──────────────────────────────────────────────────────────────


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    default_workspace: Path = field(default_factory=Path.cwd)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    allow_plaintext: bool = False
    allowed_origins: tuple[str, ...] = ()
    command: list[str] = field(default_factory=bridge_command)
    #: The URL clients should use (pairing link, QR code), when the listen
    #: address is not it: a Tailscale or DNS name, a tunnel, a proxy.
    public_url: str | None = None

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}

    @property
    def scheme(self) -> str:
        return "wss" if self.tls_cert else "ws"

    def url(self, host: str | None = None) -> str:
        return f"{self.scheme}://{host or self.host}:{self.port}/v1"

    def advertised_url(self) -> str:
        """The URL to give clients: ``public_url``, else a reachable address.

        A wildcard listen address (``0.0.0.0``) is not one a phone can dial, so
        it is replaced by this machine's first LAN address.
        """
        if self.public_url:
            return self.public_url
        if self.host in {"0.0.0.0", "::", ""}:
            addresses = lan_addresses()
            if addresses:
                return self.url(addresses[0])
        return self.url()


def lan_addresses() -> list[str]:
    """This machine's non-loopback IPv4 addresses (best effort)."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    try:
        # The address of the default route, without sending anything.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.add(str(probe.getsockname()[0]))
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


class ConfigError(ValueError):
    pass


def check_config(config: GatewayConfig) -> None:
    if bool(config.tls_cert) != bool(config.tls_key):
        raise ConfigError("--tls-cert and --tls-key go together.")
    if not config.is_loopback and not config.tls_cert and not config.allow_plaintext:
        raise ConfigError(
            f"Refusing to listen on {config.host} without TLS: a paired device can "
            "run commands on this machine. Pass --tls-cert/--tls-key, or "
            "--allow-plaintext on a network that already encrypts (Tailscale, a "
            "VPN, an SSH tunnel)."
        )
    if config.public_url and not config.public_url.startswith(("ws://", "wss://")):
        raise ConfigError("--public-url must start with ws:// or wss://.")
    if not config.default_workspace.is_dir():
        raise ConfigError(f"Workspace {config.default_workspace} is not a directory.")


class Gateway:
    def __init__(self, config: GatewayConfig, store: DeviceStore | None = None) -> None:
        self.config = config
        self.store = store or DeviceStore()
        self.hosts: dict[Path, WorkspaceHost] = {}
        self._failures: dict[str, deque[float]] = {}
        self.log_dir = remote_dir().parent / "logs"

    # ── Rate limiting ──────────────────────────────────────────────────────
    def _blocked(self, address: str) -> bool:
        window = self._failures.get(address)
        if not window:
            return False
        cutoff = time.time() - FAILURE_WINDOW_S
        while window and window[0] < cutoff:
            window.popleft()
        return len(window) >= MAX_FAILURES

    def _fail(self, address: str) -> None:
        self._failures.setdefault(address, deque()).append(time.time())

    # ── Connection handling (transport-agnostic) ───────────────────────────
    async def handle(self, recv: Any, send: Any, address: str) -> None:
        """Serve one connection; *recv* returns the next text frame or None."""
        if self._blocked(address):
            await send(_error(None, _ERR_AUTH, "too many failed attempts; try later"))
            return
        try:
            raw = await asyncio.wait_for(recv(), AUTH_TIMEOUT_S)
        except TimeoutError:
            return
        device_id = await self._authenticate(raw, send, address)
        if device_id is None:
            return
        client = Client(send, device_id, address)
        host: WorkspaceHost | None = None
        try:
            while True:
                raw = await recv()
                if raw is None or client.closed:
                    break
                message = self._parse(raw)
                if message is None:
                    continue
                method = str(message.get("method") or "")
                request_id = message.get("id")
                if method == "gateway.info":
                    await client.send(_result(request_id, self.info(host)))
                elif method == "gateway.open":
                    if host is not None:
                        await client.send(
                            _error(request_id, _ERR_STATE, "workspace already open")
                        )
                        continue
                    host = await self._open(client, message)
                elif method.startswith("gateway."):
                    await client.send(
                        _error(request_id, -32601, f"unknown method: {method}")
                    )
                elif host is None:
                    await client.send(
                        _error(request_id, _ERR_STATE, "call gateway.open first")
                    )
                else:
                    await host.forward(client, message)
        finally:
            client.closed = True
            if host is not None:
                host.detach(client)

    @staticmethod
    def _parse(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return message if isinstance(message, dict) else None

    async def _authenticate(self, raw: Any, send: Any, address: str) -> str | None:
        message = self._parse(raw) or {}
        method = message.get("method")
        params = message.get("params") or {}
        request_id = message.get("id")
        if method == "gateway.auth":
            device = self.store.verify(str(params.get("token") or ""))
            if device is not None:
                log.info(
                    "device %s (%s) connected from %s", device.id, device.name, address
                )
                await send(
                    _result(request_id, {"deviceId": device.id, "name": device.name})
                )
                return device.id
        elif method == "gateway.pair":
            if self.store.consume_pairing_code(str(params.get("code") or "")):
                name = str(params.get("deviceName") or "device")
                device, token = self.store.add_device(name)
                log.info(
                    "paired device %s (%s) from %s", device.id, device.name, address
                )
                await send(_result(request_id, {"deviceId": device.id, "token": token}))
                return device.id
        self._fail(address)
        log.warning("failed %s from %s", method or "handshake", address)
        await send(_error(request_id, _ERR_AUTH, "authentication failed"))
        return None

    async def _open(
        self, client: Client, message: dict[str, Any]
    ) -> WorkspaceHost | None:
        params = message.get("params") or {}
        request_id = message.get("id")
        raw_path = params.get("workspace")
        path = (
            Path(str(raw_path)).expanduser()
            if raw_path
            else self.config.default_workspace
        )
        try:
            path = path.resolve()
        except OSError:
            pass
        if not path.is_dir():
            await client.send(
                _error(request_id, _ERR_WORKSPACE, f"not a directory: {path}")
            )
            return None
        host = self.hosts.get(path)
        if host is None:
            host = WorkspaceHost(path, list(self.config.command), self.log_dir)
            self.hosts[path] = host
        try:
            await host.ensure_running()
        except OSError as exc:
            await client.send(_error(request_id, _ERR_BRIDGE, f"bridge failed: {exc}"))
            return None
        resumed, missed = host.attach(client, bool(params.get("resume")))
        await client.send(
            _result(
                request_id,
                {
                    "workspace": str(path),
                    "protocol": PROTOCOL_VERSION,
                    "serverVersion": _engine_version(),
                    "resumed": resumed,
                },
            )
        )
        for line in missed:
            await client.send(line)
        return host

    def info(self, host: WorkspaceHost | None) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "serverVersion": _engine_version(),
            "defaultWorkspace": str(self.config.default_workspace),
            "workspace": str(host.path) if host else None,
            "workspaces": [str(p) for p, h in self.hosts.items() if h.running],
        }

    async def close(self) -> None:
        await asyncio.gather(*(h.close() for h in self.hosts.values()))


# ── WebSocket server ─────────────────────────────────────────────────────────


def _ssl_context(config: GatewayConfig) -> ssl.SSLContext | None:
    if not config.tls_cert:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(config.tls_cert), str(config.tls_key))
    return context


def server_info_path() -> Path:
    return remote_dir() / "server.json"


async def serve(config: GatewayConfig, *, ready: Any = None) -> None:
    """Run the gateway until cancelled. *ready* (optional) gets the bound port."""
    from websockets.exceptions import ConnectionClosed
    from websockets.asyncio.server import ServerConnection
    from websockets.asyncio.server import serve as ws_serve

    check_config(config)
    gateway = Gateway(config)

    def process_request(connection: ServerConnection, request: Any) -> Any:
        if request.path.split("?", 1)[0] != "/v1":
            return connection.respond(404, "Not found\n")
        origin = request.headers.get("Origin")
        if origin and origin not in config.allowed_origins:
            return connection.respond(403, "Origin not allowed\n")
        return None

    async def handler(connection: ServerConnection) -> None:
        address = str((connection.remote_address or ("?",))[0])

        async def recv() -> Any:
            try:
                return await connection.recv()
            except ConnectionClosed:
                return None

        async def send(line: str) -> None:
            await connection.send(line)

        await gateway.handle(recv, send, address)

    async with ws_serve(
        handler,
        config.host,
        config.port,
        ssl=_ssl_context(config),
        process_request=process_request,
        max_size=MAX_FRAME_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        config.port = port
        info = {
            "url": config.advertised_url(),
            "pid": os.getpid(),
            "startedAt": time.time(),
            "tls": bool(config.tls_cert),
        }
        path = server_info_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(info) + "\n", encoding="utf-8")
        if ready is not None:
            ready(port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGHUP) if os.name == "posix" else ():
            # A service manager stops us with SIGTERM: shut the bridges down
            # and remove server.json, as Ctrl+C does.
            loop.add_signal_handler(signum, stop.set)
        try:
            await stop.wait()
        finally:
            path.unlink(missing_ok=True)
            await gateway.close()


__all__ = [
    "DEFAULT_PORT",
    "PROTOCOL_VERSION",
    "ConfigError",
    "Gateway",
    "GatewayConfig",
    "WorkspaceHost",
    "bridge_command",
    "check_config",
    "serve",
    "server_info_path",
]
