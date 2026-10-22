# Phoson Monitor Plugin

Long-running background monitors that outlive the current agent run and
**re-activate the agent** when their condition fires (IMPROVEMENTS.md
[I-126](https://github.com/phoson-lat/phoson-engine-minimal/issues/126)).

The engine is stateless-by-run: when `AgentEngine.run()` returns, nothing
reschedules itself. This plugin fills that gap using only the canonical
`Plugin` contract — no new engine lifecycle.

## Monitor kinds

| Kind | Spec | Fires when |
|------|------|------------|
| `interval` | `{"seconds": N, "once": bool}` | N seconds pass (once), or every N seconds |
| `file` | `{"path": p, "event": "created"\|"changed"\|"both", "poll_seconds": N}` | a path/glob appears, or its mtime/size changes |
| `command` | `{"command": cmd, "interval_seconds": N, "timeout_seconds": M}` | the shell command exits non-zero, times out, or its output changes |

Files are **polled** (no inotify) so the plugin stays stdlib-only and
cross-platform. `command` output is captured (stdout+stderr, tail up to
4000 chars) and compared between ticks.

## Tools

- `register_monitor(name, kind, spec)` — start a monitor. The registration
  is a tool call, so it passes the host's permission gate with the exact
  spec visible.
- `list_monitors()` — state, last fire, pending wake counts.
- `stop_monitor(name)` — cancel the task and remove the monitor.

## Wake mechanism

Every fire lands in a **persistent queue** (`wake.jsonl`) — the source of
truth — carrying the original `session_id` so a host can resume the same
conversation tree. On top of the queue:

- **`on_wake` callback** (optional, via `configure({"on_wake": fn})`):
  invoked fire-and-forget on every fire, for hosts with a live event loop
  (e.g. an embedded runtime starting a new run immediately). The callback
  failing never loses the queued event.
- **Queue drain**: the Phoson CLI consumes pending wakes for the current
  session at the start of the next user turn and prepends them to the user
  message (`[MONITOR EVENTS] …`), so the agent acts on findings in context.
- **Autonomous wake**: the interactive CLI (classic + full-screen) also
  runs a wake loop — when a monitor fires while the agent is **idle**, the
  pending wakes trigger a turn of their own (a `[MONITOR EVENTS]` user
  message the agent acts on), with no user input required. Wakes that
  arrive *mid-run* are folded into the user's next turn instead.

See `examples/monitor_wake_host.py` for a standalone host that resumes the
same `ConversationTree` from `JsonlStorage` when woken.

## Persistence & crash semantics

State lives under `data_dir` (default `~/.phoson/monitors/`):

- `monitors.json` — registry (definitions + state), atomic writes.
- `wake.jsonl` — fired events, rewritten atomically on every mutation
  (the queue is bounded by the per-monitor cap).

The **disk is the source of truth; in-memory tasks are a cache**. If the
host dies (or rebuilds its engine, e.g. `/model`), tasks disappear, but
monitors still say `running` on disk and the next `initialize()` +
`ensure_started()` resurrects them. Pending wakes survive and are drained
by whichever host takes over. One writer per `data_dir` is assumed
(multi-process locking is a follow-up).

## Working-directory scope

Every monitor records the working directory it was registered in (from
the `cwd_provider` in the tool context, else the host's, else the process
cwd). `command` monitors run there and relative `file` paths resolve
against it. A host that sets a `cwd_provider` only lists, runs and stops
the monitors of its current directory, so the monitors of `A/` are
invisible from `B/`. Names are unique per directory (registry key
`name@cwd`). Monitors registered before scoping have no cwd and stay
visible everywhere, so they can still be stopped.

## Configuration

```python
engine = AgentEngine(
    chat=chat,
    plugins=[
        {
            "name": "phoson-plugin-monitor",
            "config": {
                "data_dir": "~/.phoson/monitors",  # default
                "on_wake": my_wake_callback,       # optional, live hosts
                "max_pending_wakes": 5,            # per-monitor anti-storm cap
                "default_session_id": "",          # when the host injects none
                "cwd_provider": lambda: session_cwd, # optional: scope to a dir
            },
        }
    ],
)
```

Host integration (duck-typed, optional):

- `await plugin.ensure_started()` — (re)spawn tasks for the running
  monitors in scope (and stop the out-of-scope ones); call after
  building/rebuilding the engine.
- `plugin.set_cwd_provider(fn)` / `await plugin.reconcile_scope()` — scope
  the plugin to a directory and follow changes to it (the CLI wake loop
  calls `reconcile_scope()` every tick).
- `plugin.drain_pending_wakes(session_id)` — consume pending wakes before a
  run (the CLI does this in `run_turn`).
- `plugin.get_commands()` — contributes the `/monitors` slash command
  (list, per-monitor detail, `pending`) via the I-110 CLI extension
  contract.

Enabling in the CLI: set `enable_monitors = true` in `config.toml` (or
`PHOSON_ENABLE_MONITORS=1`).

## Security

`command` monitors run a shell command **unattended** on an interval. The
registration is permission-gated (you approve the exact command once),
but every subsequent tick re-runs it without prompting — treat a
registered monitor as standing shell access until `stop_monitor`.

## Development

```bash
pytest tests/phoson_plugin_monitor -q
```

Unit tests run against fakes (fake clock, `tmp_path`, fake chat) with no
network or real timers; one integration test uses a real 1-second
`interval` monitor and is skipped under `--fast`.
