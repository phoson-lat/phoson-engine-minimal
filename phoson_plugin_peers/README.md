# phoson-plugin-peers

Named agents that talk to each other **across CLI windows**. Each window is a
full Phoson session (its own project, history, permissions and terminal);
the plugin gives it a name and a mailbox.

```bash
# terminal 1 (in the API repo)
phoson-cli --name backend-agent

# terminal 2 (in the web repo)
phoson-cli --name frontend-agent
> dile al backend-agent que te pase la doc de la API
```

The frontend's model calls `peer_ask("backend-agent", "…")`. The backend
window wakes up, shows `📨 frontend-agent asks you` with the message, works on
it with its own tools, and its **final answer of that turn is sent back
automatically** — it becomes the result of the frontend's `peer_ask`.

## Tools

| Tool | Behaviour |
|---|---|
| `peer_list()` | Other agents on the team, their state (`idle`/`busy`/`waiting on X`) and cwd. |
| `peer_ask(to, content, timeout=600)` | Send a request and **wait** for the answer (the recipient's final turn answer). Fails fast when the peer is offline or would deadlock. |
| `peer_send(to, content)` | Fire-and-forget. Wakes the recipient; an answer, if any, arrives later as a new message. Queued if the recipient is offline. |

Slash commands: `/peers` (list) and `/tell <agent> <text>` (message an agent
yourself, without going through your model).

## Configuration

| Flag / env / `config.toml` | Default | Meaning |
|---|---|---|
| `--name` / `PHOSON_PEER_NAME` / `peer_name` | — | This window's agent name. Empty = feature off. |
| `--team` / `PHOSON_PEER_TEAM` / `peer_team` | `default` | Namespace; only agents on the same team see each other. |
| `PHOSON_PEERS_DIR` / `peers_data_dir` | `~/.phoson/peers` | Shared state directory. |
| `PHOSON_PEERS_MAX_HOPS` / `peers_max_hops` | `4` | Max agent-to-agent forwarding depth. |

Names: 1–64 chars of `a-z 0-9 . _ -`, case-insensitive. A name held by a live
window is refused at startup; a crashed window's name is taken over once its
heartbeat (every 5 s) is 30 s old.

## How it works

```
~/.phoson/peers/<team>/
  agents/<name>.json        presence: pid, cwd, state, waiting_on, heartbeat
  inbox/<name>/tmp|new|cur  one JSON file per message
```

- **Transport.** The sender writes into the recipient's `tmp/` and publishes
  with an atomic `os.replace` into `new/`; no cross-process locks. Moving
  `new/ → cur/` is the read receipt.
- **Delivery = wakes.** The plugin is a wake provider (`pending_wakes` /
  `drain_pending_wakes`, like monitors and background jobs). The CLI's wake
  loop runs an autonomous turn when the agent is idle; a busy agent gets the
  messages folded into its next turn.
- **Auto-reply.** The host calls `on_turn_end(outcome)` after every turn; the
  plugin replies to each request drained by that turn with the final answer
  (or an error if the turn failed/was cancelled, so the asker never hangs).
- **Loop guard.** Messages carry `hops`; anything sent while handling a
  message with `hops=h` gets `h+1`, refused above `peers_max_hops`. A user
  turn resets the depth. `peer_ask` refuses to ask an agent that is waiting
  on you (deadlock).

## Security

Peer messages are **untrusted input** from another agent: they are framed as a
colleague's request, and the recipient runs them under *its own* permission
policy (an `ask` tool prompts in the recipient's window). Every window on the
same team and machine can message every other — there is no allowlist yet.

## Limits (MVP)

- No interruption: a busy agent reads new messages when its current turn ends.
- One reply per request; streaming partial answers is not supported.
- Same-machine (or shared filesystem) only.
