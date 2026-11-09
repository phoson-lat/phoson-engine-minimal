"""``phoson-cli serve`` and ``phoson-cli remote …`` (remote-access gateway)."""

import sys
import json
import time
import socket
import asyncio
import logging
import argparse
from pathlib import Path
from urllib.parse import urlencode

from .devices import PAIRING_TTL_S, DeviceStore

SERVE_HELP = """\
Usage: phoson-cli serve [options]

Serve this machine's phoson-cli to paired devices (Phoson Desktop in remote
mode, the mobile app) over WebSocket. Each workspace runs in its own bridge
process, as in Phoson Desktop.

Options:
  --host HOST          Address to listen on (default 127.0.0.1)
  --port PORT          Port (default 7337; 0 = any free port)
  --workspace DIR      Workspace for clients that do not ask for one
                       (default: the current directory)
  --tls-cert FILE      TLS certificate (PEM); with --tls-key, serves wss://
  --tls-key FILE       TLS private key (PEM)
  --allow-plaintext    Allow a non-loopback --host without TLS (only on a
                       network that already encrypts: Tailscale, VPN, SSH)
  --allow-origin URL   Accept browser connections from this origin
                       (repeatable; browsers are refused by default)
  --pair               Print a pairing code at startup

Needs the `remote` extra: phoson-cli extras install remote
"""

REMOTE_HELP = """\
Usage: phoson-cli remote pair         Create a pairing code for a new device
       phoson-cli remote devices      List paired devices
       phoson-cli remote revoke <id>  Remove a device (it can no longer connect)
"""


def _pair_uri(url: str | None, code: str) -> str:
    query = {"code": code}
    if url:
        query["url"] = url
    return f"phoson://pair?{urlencode(query)}"


def _print_pairing(store: DeviceStore, url: str | None) -> None:
    code, expires = store.create_pairing_code()
    minutes = int(PAIRING_TTL_S // 60)
    print(f"Pairing code: {code}   (single use, expires in {minutes} min)")
    if url:
        print(f"Server:       {url}")
    print(f"Link:         {_pair_uri(url, code)}")
    _ = expires


def _lan_hint(host: str, port: int, scheme: str) -> list[str]:
    if host not in {"0.0.0.0", "::"}:
        return []
    try:
        name = socket.gethostname()
        addresses = sorted(
            {
                info[4][0]
                for info in socket.getaddrinfo(name, None, socket.AF_INET)
                if not str(info[4][0]).startswith("127.")
            }
        )
    except OSError:
        return []
    return [f"{scheme}://{address}:{port}/v1" for address in addresses]


def run_serve(argv: list[str]) -> int:
    if any(arg in {"-h", "--help", "help"} for arg in argv):
        print(SERVE_HELP, end="")
        return 0
    try:
        import websockets  # noqa: F401
    except ImportError:
        print(
            "phoson-cli serve needs the `remote` extra: "
            "phoson-cli extras install remote",
            file=sys.stderr,
        )
        return 1
    from .gateway import DEFAULT_PORT, ConfigError, GatewayConfig, serve

    parser = argparse.ArgumentParser(prog="phoson-cli serve", add_help=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--allow-plaintext", action="store_true")
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument("--pair", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(SERVE_HELP, end="", file=sys.stderr)
        return 2

    config = GatewayConfig(
        host=args.host,
        port=args.port,
        default_workspace=args.workspace.expanduser().resolve(),
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        allow_plaintext=args.allow_plaintext,
        allowed_origins=tuple(args.allow_origin),
    )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    def ready(port: int) -> None:
        print(f"phoson-cli serve: listening on {config.url()}")
        for url in _lan_hint(config.host, port, config.scheme):
            print(f"  reachable at {url}")
        print(f"  default workspace: {config.default_workspace}")
        if not config.tls_cert and not config.is_loopback:
            print("  WARNING: plaintext; use only on an encrypted network.")
        if args.pair:
            _print_pairing(DeviceStore(), config.url())
        else:
            print("  pair a device with: phoson-cli remote pair")
        sys.stdout.flush()

    try:
        asyncio.run(serve(config, ready=ready))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(
            f"Error: cannot listen on {config.host}:{config.port}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


def run_remote(argv: list[str]) -> int:
    from .gateway import server_info_path

    store = DeviceStore()
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(REMOTE_HELP, end="")
        return 0 if argv else 2
    command, *rest = argv
    if command == "pair" and not rest:
        url = None
        try:
            url = json.loads(server_info_path().read_text(encoding="utf-8")).get("url")
        except (OSError, ValueError):
            print("(no running `phoson-cli serve` found; the code works once it runs)")
        _print_pairing(store, url)
        return 0
    if command == "devices" and not rest:
        devices = store.devices()
        if not devices:
            print("No paired devices.")
        for device in devices:
            seen = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(device.last_seen))
                if device.last_seen
                else "never"
            )
            print(f"{device.id}  {device.name:<24}  last seen {seen}")
        return 0
    if command in {"revoke", "remove"} and len(rest) == 1:
        if store.revoke(rest[0]):
            print(f"Revoked {rest[0]}. It can no longer connect.")
            return 0
        print(f"No paired device with id {rest[0]}.", file=sys.stderr)
        return 1
    print(REMOTE_HELP, end="", file=sys.stderr)
    return 2


__all__ = ["run_remote", "run_serve"]
