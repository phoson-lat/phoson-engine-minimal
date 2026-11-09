# Writing a remote client (Phoson Mobile, Desktop remote mode)

This guide is for building a client that connects to `phoson-cli serve`: the
Phoson Mobile app first, and Phoson Desktop's remote mode later. It covers the
server setup for development, the connection handshake, the session and turn
lifecycle, confirmations, reconnection, and what the app must do to stay
secure. Operating the server is covered in [cli/remote.md](cli/remote.md).

Everything here is verified against `phoson_bridge/gateway.py` (the gateway)
and `phoson_bridge/server.py` (the bridge). Phoson Desktop's typed client
(`Phoson-Desktop/src/bridge/protocol.ts`) is a working reference for the
payload shapes.

## 1. The picture

```
Phoson Mobile ──WebSocket──▶ phoson-cli serve ──stdio──▶ phoson-cli bridge (one per workspace)
   (client)                     (gateway: auth,             (sessions, agent, tools;
                                 routing, mailbox)            the same process Desktop runs)
```

- The **agent runs on the server**: models, API keys, files and tools are
  there. The app only sends input and renders what the agent streams back.
- The **gateway** authenticates devices and forwards protocol lines. It does
  not interpret agent traffic.
- The **bridge** is the JSON-RPC server Phoson Desktop already uses locally.
  Every method in this guide that does not start with `gateway.` is a bridge
  method.

## 2. A server for development

```bash
phoson-cli extras install remote        # once: installs websockets
cd ~/code/my-project                    # the default workspace
phoson-cli serve --pair                 # 127.0.0.1:7337 + a pairing code
```

A phone cannot reach loopback. Pick one of these options:

| Setup | Command | Notes |
|---|---|---|
| **Tailscale** (recommended) | `phoson-cli serve --host <tailscale-ip> --allow-plaintext` | Tailscale already encrypts, so `ws://` is acceptable. It works on cellular too. |
| LAN with TLS | `phoson-cli serve --host 0.0.0.0 --tls-cert c.pem --tls-key k.pem` | The phone must trust the certificate. A self-signed one needs pinning in the app (section 9). |
| LAN, plaintext | `phoson-cli serve --host 0.0.0.0 --allow-plaintext` | Only on a network you trust, for quick development. |
| Emulator / simulator | `phoson-cli serve` | The iOS simulator reaches the host as `127.0.0.1`; the Android emulator as `10.0.2.2`. |

Platform rules for `ws://` (no TLS):

- **iOS:** App Transport Security blocks plaintext connections. In development,
  add an `NSAppTransportSecurity` exception (`NSAllowsLocalNetworking`, or a
  per-domain exception). Release builds should use `wss://` or Tailscale with a
  narrow exception.
- **Android:** cleartext traffic is off by default since API 28. Allow it per
  domain with a `network_security_config` in development builds only.

Get a fresh code at any time with `phoson-cli remote pair`. See paired devices
with `phoson-cli remote devices`, and revoke one with `phoson-cli remote revoke
<id>`.

## 3. Transport

- **URL:** `ws://host:port/v1`, or `wss://` with TLS. Any other path returns 404.
- **Frames:** one JSON object per WebSocket **text** frame, never batched.
- **Shape:** JSON-RPC 2.0.
  - **Requests** carry an `id` (number or string) and get exactly one
    response with the same `id`.
  - **Notifications** have a `method` and no `id`, and get no answer. The server
    sends many of them: everything the agent streams.
- **Size:** frames up to 64 MiB. Uploads are base64 and capped by the bridge
  (section 7.4).
- **Keepalive:** the server pings every 20 s and drops a peer that does not
  answer within 20 s. WebSocket libraries answer pings automatically; React
  Native's built-in `WebSocket` does too.
- **No `Origin` header.** The gateway refuses browser connections, so a client
  that sends `Origin` gets **403**. React Native does not send one. A web build
  of the app would need the server started with `--allow-origin <origin>`.

```jsonc
// request
{"jsonrpc": "2.0", "id": 7, "method": "turn.run", "params": {"sessionId": "s1", "text": "hola"}}
// response
{"jsonrpc": "2.0", "id": 7, "result": {"status": "done", "errorCode": null, "finalContent": "…"}}
// error
{"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "…"}}
// notification
{"jsonrpc": "2.0", "method": "agent.event", "params": {"sessionId": "s1", "event": {"type": "AgentTokenEvent", "content": "Ho"}}}
```

## 4. Pairing

`phoson-cli remote pair` and `serve --pair` print a code such as `W2NC-K3VN`
and a link:

```
phoson://pair?code=W2NC-K3VN&url=ws%3A%2F%2F100.101.102.103%3A7337%2Fv1
```

`phoson-cli` draws this link as a **QR code** in the terminal, so the app's
pairing screen should offer a QR scanner, with manual entry of the code and URL
as a fallback. The `url` in the link is the server's advertised address: its LAN
IP when it listens on `0.0.0.0`, or its `--public-url`. Codes are case- and separator-insensitive, single use, and expire
after 10 minutes. A wrong guess counts against every pending code, so they
burn after 5 failed tries.

On a new connection, send `gateway.pair` as the **first** message:

```jsonc
→ {"jsonrpc":"2.0","id":1,"method":"gateway.pair","params":{"code":"W2NC-K3VN","deviceName":"Abel's iPhone"}}
← {"jsonrpc":"2.0","id":1,"result":{"deviceId":"05b9ac986086","token":"<43 chars>"}}
```

Store `token` in secure storage (iOS Keychain, Android Keystore through
EncryptedSharedPreferences, or `expo-secure-store`). The server keeps only its
hash, so this is the only time it is visible. The connection is now
authenticated: continue with `gateway.open`.

## 5. Connecting

Every later connection starts with `gateway.auth` within **10 seconds**:

```jsonc
→ {"jsonrpc":"2.0","id":1,"method":"gateway.auth","params":{"token":"<token>"}}
← {"jsonrpc":"2.0","id":1,"result":{"deviceId":"05b9ac986086","name":"Abel's iPhone"}}
```

Then open a workspace:

```jsonc
→ {"jsonrpc":"2.0","id":2,"method":"gateway.open","params":{"workspace":"/home/abel/code/app","resume":false}}
← {"jsonrpc":"2.0","id":2,"result":{"workspace":"/home/abel/code/app","protocol":1,"serverVersion":"0.64.0","resumed":false}}
```

- `workspace` is optional. Without it you get the server's default workspace
  (the directory `serve` was started in, or `--workspace`). The path is on the
  **server**.
- **One workspace per connection.** To use two projects at once, open two
  WebSockets, just as Desktop runs one bridge process per workspace.
- Check `protocol`. This guide describes protocol **1**. Refuse, or degrade
  gracefully, when it is greater than the newest version you support.
- `gateway.info` answers at any time after auth, with the protocol, the server
  version, the default workspace and the running workspaces. Use it to show a
  workspace picker.

Gateway errors:

| Code | Meaning | What the app should do |
|---|---|---|
| `-32001` | Authentication failed, or too many failed attempts from this address | On `gateway.auth`: the token was revoked or the server was reset, so drop it and ask the user to pair again. "too many failed attempts": wait at least 10 minutes. |
| `-32002` | Wrong state: a call before `gateway.open`, or a second `open` | A client bug. |
| `-32003` | The workspace is not a directory on the server | Ask the user for another path. |
| `-32004` | The bridge failed to start or exited mid-request | Retry. If it persists, show the error. |
| `-32601` | Unknown method | |
| `-32000` | Error raised by a bridge method; `message` explains it | Show `message`. Bridge messages are currently in Spanish. |

## 6. Sessions

Two identifiers are in play, and they are easy to confuse:

- **Saved session id** (`id` in `session.list`): the conversation stored on the
  server, in `~/.phoson/sessions`. It is stable.
- **Live session key** (`sessionId` everywhere else): a session *loaded* in
  this workspace's bridge. Every turn, event and metric is keyed by it. It
  disappears when the session is closed or the bridge restarts.

Start-up sequence:

1. `initialize` → `{config: {model, provider, …}, defaultSessionId, cwd,
   metrics, tools, commands, onboarding?}`. `defaultSessionId` is a fresh live
   session you can use right away. If `onboarding.needed` is true, the server
   has no model provider configured; tell the user to run `phoson-cli --setup`
   on the server.
2. `session.list` → `{sessions: [SessionMeta]}`, saved sessions from **every**
   workspace, newest first. Each has `id`, `title`, `updated_at`,
   `message_count`, `total_cost`, `last_model`, `cwd` and `status`. Open a
   session through a connection whose workspace is its `cwd`.
3. To continue a saved conversation, call `session.open {id}` →
   `{sessionId, cwd, sessionCwd, cwdMissing, metrics}`. The bridge then sends
   the transcript as a `session.history` notification:
   `{sessionId, messages, timestamps, toolDurations}`. `messages` are engine
   `Message`s: `{type: "Message", role, content}`, where `content` is a string
   or a list of blocks discriminated by `type` (`TextBlock`, `ToolUseBlock`,
   `ToolResultBlock`, `ImageBlock`…).
4. For a new conversation, call `session.new {}` → `{sessionId, cwd, metrics}`.
5. When leaving a session, call `session.close {sessionId}`, which frees its
   memory on the server.

## 7. Turns

### 7.1 Sending a message

```jsonc
→ {"jsonrpc":"2.0","id":9,"method":"turn.run","params":{"sessionId":"s1","text":"Revisa los tests"}}
```

`turn.run` answers **only when the turn ends**, which can take minutes. Do not
put a short timeout on it. Everything in between arrives as notifications for
that `sessionId`. The final response is
`{status, errorCode, finalContent}`, where `status` is one of `done`,
`error` or `cancelled`.

### 7.2 What streams back

| Notification | Params (besides `sessionId`) | Render as |
|---|---|---|
| `session.turn.started` | `task` | Busy state. **Every** client receives it, so a phone sees a turn that Desktop started. |
| `session.user_message` | `text`, `message` | The user bubble. It also arrives for turns sent by another client. |
| `agent.event` | `event` (below) | The live transcript. |
| `ui.flush` | none | End of a streamed text segment; close the current bubble. |
| `subagent.progress` | `progress: {tasks: [{index, task, status, input_tokens, output_tokens, cost_usd, done}]}` or `null` | A sub-agent panel; `null` clears it. |
| `session.title` | `title` | Update the session title (generated after the first turn). |
| `session.metrics` | `costUsd`, `tokens`, `inputTokens`, `outputTokens`, `steps`, `contextTokens`, `contextWindow`, `model`, `provider`, `isRunning` | The cost and context chip. |
| `session.assistant.done` | `status`, `errorCode`, `finalContent`, and `source: "wake"` on autonomous turns | The end of **every** turn, sent to all clients. Turns that no client started (a monitor, a background job or a background-task result) carry `source: "wake"`. Use it rather than the `turn.run` response to end the busy state; it also reaches clients that did not send the turn. |
| `notify` | `kind` (`info`/`warn`/`error`), `message` | A toast. |
| `plugin.block` / `plugin.block.remove` | `blockId`, `block`, `replace` / `blockId` | Live cards from plugins (`NoticeBlock`, `KeyValueBlock`, `TodoListBlock`, `ProgressBlock`). |
| `confirm.request` | see section 8 | A permission or question card. |

`agent.event` payloads are engine events, discriminated by `event.type`:

| `type` | Fields | Notes |
|---|---|---|
| `AgentTokenEvent` | `content` | Answer text. Tokens are coalesced into ~30 ms batches, so append `content`. |
| `AgentReasoningEvent` | `content` | Model reasoning; show it collapsed. Also batched. |
| `AgentToolComposingEvent` | `index`, `tool_call_id`, `tool_name`, `args_chunk` | The model is still writing a tool call. Optional to render. |
| `AgentToolStartEvent` | `index`, `tool_call_id`, `tool_name`, `args`, `label` | A tool card in the running state. |
| `AgentToolDoneEvent` | `tool_call_id`, `tool_name`, `result`, `error`, `duration_ms`, `label` | Close the matching card. `result` can be long, so truncate it. |
| `AgentStepDoneEvent` | `step` (a `RunStep` with usage and cost) | Per-step accounting. |
| `AgentRetryEvent` | `attempt`, `max_attempts`, `reason`, `code` | "Retrying…". |
| `AgentErrorEvent` | `message`, `code`, `retryable` | An error banner. |
| `AgentDoneEvent` | `result` (the run result) | The end of the agent run. The `turn.run` response follows. |

Unknown `type`s and unknown notifications **must be ignored**. New ones will be
added without a protocol version bump.

### 7.3 Cancelling

`turn.cancel {sessionId}` → `{cancelled: bool}`. The pending `turn.run` then
resolves with `status: "cancelled"`. This is the app's Esc button.

### 7.4 Photos and files from the phone

Paths in the protocol are **server** paths, so a file on the phone has to travel
as content:

- **Images and documents for the model:** call `attachment.push {sessionId,
  name, data: <base64>}`, up to 25 MB. They are attached to the next
  `turn.run`; `attachments.changed` reports the pending list, and
  `attachment.list` / `attachment.remove` / `attachment.clear` manage it.
- **Other files the agent should read:** call `upload.file {name, data}`. The
  file is written to `<workspace>/uploads/` and the result has its `relative`
  path, which you can mention in the prompt.

## 8. Confirmations and questions

When the agent needs an answer, the bridge sends
`confirm.request {sessionId, requestId, kind, …}` and waits. Answer with
`confirm.respond {requestId, …}` → `{ok}`. `ok` is false when the request was
already answered, for example by another client.

| `kind` | Request fields | Response (`confirm.respond` params) |
|---|---|---|
| `bash` | `command`, `actions` (a subset of `yes`, `always`, `no`) | `{requestId, decision: "yes" \| "always" \| "no"}` |
| `questions` | `title`, `questions: [{id, header, question, options: [{id, label, description}], multi_select, allow_other}]` | `{requestId, selections: {questionId: [optionId…]}, other: {questionId: text}}`, or `{requestId, cancelled: true}` |
| `select` | `title`, `message`, `choices: [{id, label, detail}]` | `{requestId, choice: id}` or `{requestId, cancelled: true}` |
| `form` | `title`, `message`, `fields: [{id, label, kind, required, default, help}]` | `{requestId, values: {fieldId: value}}` or `{requestId, cancelled: true}` |

- `bash` also covers non-shell tools that ask, such as an MCP tool or a file
  write outside the workspace; `command` is then a one-line summary.
- `always` saves an allow rule **on the server**, as the terminal does. Offer it
  only when it is in `actions`.
- Consider requiring biometrics before sending `yes` or `always`.
- **Do not hide the card on a timer.** The agent waits as long as it takes, and
  the phone may be the only place someone can answer.

## 9. Reconnecting

Phones drop connections all the time. On reconnect:

1. Open a new WebSocket and send `gateway.auth`.
2. Send `gateway.open {workspace, resume: true}`.
3. If `resumed` is **true**, the gateway first replays every notification
   you missed (up to 15 minutes and 5000 lines), then live traffic follows.
   Your live session keys are still valid.
4. If `resumed` is **false** (too long away, too much traffic, or the server
   restarted), discard the local live state. Call `session.list`, then
   `session.open {id}` for the conversation on screen; it replays the history.

Rules:

- **Requests in flight at disconnect are lost.** Their responses are not
  replayed. Treat them as failed, but do **not** re-send `turn.run` blindly:
  the turn probably kept running on the server. After resuming, the events
  show where it is.
- `gateway.bridgeExited {code}` means the workspace's bridge process died.
  Live session keys are gone. The next request restarts it, and you get
  `gateway.bridgeRestarted`; re-open the sessions as in step 4.
- Use exponential backoff (1 s, 2 s, 4 s … up to 30 s), and reconnect at once
  when the app returns to the foreground.

## 10. Several clients at once (current limits)

The gateway lets a phone and a desktop attach to the same workspace: every
client receives every notification, and request ids are rewritten so each
response reaches its sender. The bridge, however, was written for a single
client, so in protocol 1:

- `session.open` **always loads a new live session**, even when another client
  has the same saved session open. Two live copies of one conversation do not
  see each other's turns. The saved file merges correctly, but the screens do
  not update live.
- An answered `confirm.request` is not retracted on the other clients. Their
  card stays until the user taps it, and then `confirm.respond` returns
  `ok: false`.
- There is no method to list the sessions that are live in a bridge.

The planned additions (`session.attach`, `confirm.resolved`, `session.live`)
are tracked for the mobile proof of concept. Until they land, design the app
for the common case: the phone is the only client of the sessions it opens.

## 11. Methods that do not make sense remotely

The bridge also serves Desktop features that assume the client and the server
are the same machine. A mobile client should avoid these, or use them knowing
that they act on the server:

- `stt.*` records from the **server's** microphone. Dictate on the phone with
  the platform's speech recognition, and send text.
- `auth.login` starts an OAuth flow whose callback goes to the server's
  `localhost`. Configure providers on the server instead (`phoson-cli
  --setup`).
- `fs.*` browses and edits the server's files. This is useful as a read-only
  viewer, but it is not the phone's file system.
- `plugins.install`, `mcp.save`, `config.set`: settings changes on the server.
  Keep them out of the first version.

## 12. Security checklist for the app

- Keep the device token in secure storage only. Never put it in logs, crash
  reports or analytics.
- Show which server and workspace the app is connected to.
- On `-32001` during `gateway.auth`, the device was revoked. Delete the token
  and offer to pair again.
- Prefer `wss://`. With a self-signed certificate, pin its SHA-256 fingerprint,
  entered or scanned at pairing time, instead of disabling validation.
- A paired phone can run any command the agent can. Ask for biometrics before
  approving a `bash` confirmation, and offer a "lock app" setting.

## 13. Minimal client (TypeScript, React Native)

```ts
type Pending = { resolve: (v: any) => void; reject: (e: Error) => void };

export class PhosonClient {
  private ws!: WebSocket;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  onNotification: (method: string, params: any) => void = () => {};

  async connect(url: string, token: string, workspace?: string, resume = false) {
    this.ws = new WebSocket(url);
    await new Promise<void>((ok, fail) => {
      this.ws.onopen = () => ok();
      this.ws.onerror = () => fail(new Error("connection failed"));
    });
    this.ws.onmessage = (frame) => this.receive(JSON.parse(String(frame.data)));
    this.ws.onclose = () => {
      for (const p of this.pending.values()) p.reject(new Error("disconnected"));
      this.pending.clear();
    };
    await this.call("gateway.auth", { token });
    return this.call("gateway.open", { workspace, resume });
  }

  call(method: string, params: object = {}): Promise<any> {
    const id = this.nextId++;
    this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  private receive(msg: any) {
    if (msg.id !== undefined && msg.method === undefined) {
      const p = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) p?.reject(Object.assign(new Error(msg.error.message), msg.error));
      else p?.resolve(msg.result);
      return;
    }
    if (msg.method) this.onNotification(msg.method, msg.params ?? {});
  }
}

// const client = new PhosonClient();
// client.onNotification = (method, p) => {
//   if (method === "agent.event" && p.event.type === "AgentTokenEvent") append(p.sessionId, p.event.content);
//   if (method === "confirm.request") showCard(p);
// };
// await client.connect(url, token);
// const { defaultSessionId } = await client.call("initialize");
// await client.call("turn.run", { sessionId: defaultSessionId, text: "hola" });
```

Pairing is the same `call`, with `gateway.pair` in place of `gateway.auth`, on
a fresh connection.

## 14. Trying it without an app

```bash
phoson-cli serve --pair            # terminal 1; note the code and the URL
uv run python - <<'EOF'            # terminal 2, from the engine repo
import asyncio, json
from websockets.asyncio.client import connect

URL, CODE = "ws://127.0.0.1:7337/v1", "XXXX-XXXX"

async def main():
    async with connect(URL) as ws:
        async def call(i, method, **params):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == i and "method" not in msg:
                    return msg
                print("  ·", msg.get("method"))
        print(await call(1, "gateway.pair", code=CODE, deviceName="script"))
        print(await call(2, "gateway.open"))
        init = (await call(3, "initialize"))["result"]
        print(await call(4, "turn.run", sessionId=init["defaultSessionId"], text="Say hi in five words"))

asyncio.run(main())
EOF
```
