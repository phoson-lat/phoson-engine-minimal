# Remote access (`phoson-cli serve`)

`phoson-cli serve` lets another device use this machine's phoson-cli: Phoson
Desktop in remote mode, or the mobile app. The agent, your files, your
configuration and your API keys stay on this machine; the client is only a
window onto it.

```bash
phoson-cli extras install remote      # once: the WebSocket server
phoson-cli serve --pair               # listen on 127.0.0.1:7337, print a code
```

## Pairing

A device pairs once with a short-lived code and gets its own token:

```text
phoson-cli remote pair          # a new code (single use, 10 minutes)
phoson-cli remote devices       # who is paired, and when they last connected
phoson-cli remote revoke <id>   # that device can no longer connect
```

`remote pair` also prints a `phoson://pair?...` link with the server address,
for apps that scan a QR code. Codes are 8 characters. A wrong guess counts
against every pending code, so they burn after 5 failed tries. Each address
also gets only 5 failed attempts every 10 minutes. Only a hash of each device
token is stored, in `~/.phoson/remote/devices.json`.

## Reaching the server

By default the server listens on loopback only, which is safe and useful
through an SSH tunnel:

```bash
ssh -L 7337:127.0.0.1:7337 my-server     # on the client machine
```

To listen on another address you need **TLS**, or you must state that the
network already encrypts:

```bash
phoson-cli serve --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
phoson-cli serve --host 100.x.y.z --allow-plaintext   # a Tailscale address
```

A paired device can do everything you can do in this terminal, including
running commands and editing files, because it drives the same agent with the
same permission rules. Pair only devices you control, and revoke a device you
lose.

Browser pages cannot connect: a request with an `Origin` header is refused
unless you allow that origin with `--allow-origin`. This stops a web page from
talking to a gateway on your machine.

## How it works

Phoson Desktop runs one bridge process per workspace and speaks JSON-RPC to
it, one JSON object per line. `phoson-cli bridge` is that same process. The
gateway starts one `phoson-cli bridge` per workspace on this machine and
forwards lines over WebSocket, one line per text frame, unchanged. A remote
client therefore speaks exactly the protocol of local Desktop.

- **Several clients at once.** A phone and a desktop can watch and drive the
  same workspace. Agent events and confirmations go to every client. Request
  ids are rewritten per client, so each response reaches only the client that
  asked.
- **Dropped connections.** When a phone changes networks, the gateway keeps the
  notifications it missed for 15 minutes (up to 5000). Reconnecting with
  `resume: true` replays them. Otherwise the client re-opens its sessions, as
  Desktop does after its bridge restarts.
- **A crashed bridge** is reported (`gateway.bridgeExited`) and restarted on
  the next request (`gateway.bridgeRestarted`).

## Protocol

Connect to `ws://host:port/v1` (`wss://` with TLS). The gateway handles these
methods itself. Every other method goes to the workspace's bridge (see
`phoson_bridge/server.py` for the full list: `turn.run`, `turn.cancel`,
`session.*`, `confirm.respond`, …).

| Method | When | Result |
|---|---|---|
| `gateway.pair {code, deviceName}` | first message, first time | `{deviceId, token}`: store the token |
| `gateway.auth {token}` | first message, afterwards | `{deviceId, name}` |
| `gateway.open {workspace?, resume?}` | once per connection | `{workspace, protocol, serverVersion, resumed}` |
| `gateway.info` | any time after auth | protocol, version, default and running workspaces |

The first message must authenticate within 10 seconds. Open one connection per
workspace, just as Desktop runs one bridge process per workspace.
