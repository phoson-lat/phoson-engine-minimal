# phoson-plugin-ssh

Run commands and move files on **remote hosts over SSH** from a Phoson
session (issue #169), without dropping out of the agent to a local shell.

## Tools

| Tool | Risk | Purpose |
|---|---|---|
| `ssh_hosts()` | read-only | List the explicitly configured host aliases (no connection). |
| `ssh_exec(host, command, cwd?, timeout_seconds?)` | mutating | Run a non-interactive command; returns stdout/stderr/exit status. |
| `ssh_copy_local_to_remote(local_path, host, remote_path)` | mutating | Copy a local file to a host over SFTP. |
| `ssh_copy_remote_to_local(host, remote_path, local_path)` | mutating | Copy a remote file to the local machine over SFTP. |

`ssh_exec` allocates **no PTY**; interactive programs (editors, `sudo`
password prompts) are not supported by design.

## Configuration

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-ssh",
            "config": {
                "hosts": {
                    "web-1": {"host": "10.0.0.11", "username": "deploy"},
                    "db": {"host": "db.internal", "port": 2222, "username": "ops"},
                },
                "command_timeout": 120,
                "max_output_chars": 8000,
            },
        }
    ],
)
```

Recognised per-host keys: `host`, `port`, `username`, `client_keys`,
`known_hosts`, `config`, `proxy_command`, `agent_forwarding`,
`connect_timeout`, `keepalive_interval`. Any other key is rejected at
`configure()` time.

Aliases **not** listed under `hosts` are resolved through `~/.ssh/config`
(so your existing names, `IdentityFile` and `ProxyJump` keep working).
Plugin config wins over `~/.ssh/config`.

### Config keys

| Key | Default | Meaning |
|---|---|---|
| `hosts` | `{}` | Alias → connection options. |
| `known_hosts` | `~/.ssh/known_hosts` | File used when an alias does not override it. Must not be empty. |
| `connect_timeout` | `15` | Seconds for a connection attempt. |
| `command_timeout` | `60` | Default seconds per command. |
| `max_output_chars` | `8000` | Cap per output stream. |
| `max_transfer_bytes` | `67108864` | (Reserved) cap for a single SFTP transfer. |

## Security

- **Strict host-key verification** against `~/.ssh/known_hosts`. The plugin
  never auto-adds a key and never sends `StrictHostKeyChecking=no`. An
  unknown host fails.
- **Key/agent auth only.** No password is ever accepted as a tool argument
  (it would end up in the transcript and the permission audit digest).
- **Permissions.** `ssh_exec`, `ssh_copy_local_to_remote` and
  `ssh_copy_remote_to_local` publish destructive/open-world risk hints, so
  they resolve to **`ask`** by default and **fail closed** in one-shot mode.
  `ssh_hosts` is read-only (`allow`). Explicitly set a looser level in
  `~/.phoson/permissions.json` if you want unattended runs.
- **Bounded.** Per-command timeout and capped output protect the context.

### Not yet: per-host allow-patterns

Allow-patterns (`ssh_exec:prod-*`) only apply to tools declared in the
host's permission match table, which plugin tools cannot extend today. Host
policy is therefore all-or-nothing via the tool-level `ask`/`allow`/`deny`.
A follow-up will add plugin-declared match-args (shared with the browser and
computer-use plugins).

## Development

```bash
uv sync --dev --extra ssh
uv run pytest tests/phoson_plugin_ssh -q
```
