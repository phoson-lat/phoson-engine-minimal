# Phoson Background Jobs Plugin

Run shell commands as **one-shot background jobs** that outlive the current
agent run and **re-activate the agent** when they finish (issue
[#217](https://github.com/phoson-lat/phoson-engine-minimal/issues/217)).

The `bash` tool is synchronous with the run: the agent waits on the command
until it finishes or times out. For long jobs — builds, tests, training,
deploys, `make` — there was no way to say *"start it and keep working; ping me
when it's done"*. This plugin fills that gap using only the canonical `Plugin`
contract — no new engine lifecycle.

> Not to be confused with `phoson_plugin_monitor`: a monitor runs a command
> **on an interval** (polling, fires on failure/output change). A background
> job is **one-shot** and fires exactly once, when it completes (exit code +
> output tail + duration). See the comparison table in #217.

## Tools

- `run_bg_job(command, name?, cwd?, env?, max_output_chars?)` — spawn a
  detached job (`start_new_session=True`, own process group) and return
  **immediately** with its id. The launch is a tool call, so it passes the
  host's permission gate with the exact command visible.
- `list_bg_jobs()` — state, elapsed time, exit code and pending wakes.
- `stop_bg_job(job_id)` — `os.killpg()` over the whole process group (no
  zombies / stuck pipes). The completion wake is still delivered, with state
  `stopped`.
- `wait_bg_jobs(job_ids?, timeout_seconds?)` — optional; block until one
  finishes or the timeout hits (handy for short jobs).

## Wake mechanism

Every completion lands in a **persistent queue** (`wakes.jsonl`) — the source
of truth — carrying the original `session_id`. On top of the queue:

- **`on_wake` callback** (optional, via `configure({"on_wake": fn})`): invoked
  fire-and-forget on every completion, for hosts with a live event loop.
- **Queue drain**: the CLI consumes pending wakes for the current session at
  the start of the next turn and prepends a `[BACKGROUND JOB EVENTS]` header,
  so the agent acts on the result in context.
- **Autonomous wake**: when a job finishes while the agent is **idle**, the
  pending wakes trigger a turn of their own.

The wake payload is
`{state, command, returncode, timed_out, duration_ms, output_tail}`.

## Persistence & crash semantics

State lives under `data_dir` (default `~/.phoson/bgjobs/`):

- `jobs.json` — registry (definitions + state), atomic writes.
- `wakes.jsonl` — completion events, rewritten atomically on every mutation.
- `jobs/<job_id>/output.log` — the full combined stdout/stderr (only the tail
  is copied into the registry/wake).

The **disk is the source of truth; in-memory tasks are a cache**. Jobs are
detached by design, so they keep running if the host dies. On the next start,
`ensure_started()` reconciles every job still marked `running`:

- alive process group (we are not its parent anymore) → polled until it exits,
  then marked `orphaned`;
- dead process group → marked `orphaned` immediately.
  We cannot read the exit code of a process we did not spawn, so orphaned
  jobs carry `returncode=null` — the point is that the agent is told the job
  ended and can inspect its log.

## Configuration

```python
engine = AgentEngine(
    chat=chat,
    plugins=[
        {
            "name": "phoson-plugin-bgjobs",
            "config": {
                "data_dir": "~/.phoson/bgjobs",  # default
                "on_wake": my_wake_callback,      # optional, live hosts
                "max_pending_wakes": 5,           # per-job anti-storm cap
                "default_session_id": "",         # when the host injects none
                "max_output_chars": 4000,         # output tail cap
                "watchdog_seconds": 0,            # 0 = no kill timeout
            },
        }
    ],
)
```

Host integration (duck-typed, optional):

- `await plugin.ensure_started()` — reconcile running jobs after building or
  rebuilding the engine.
- `plugin.drain_pending_wakes(session_id)` / `plugin.pending_wakes(session_id)`
  — consume / peek pending wakes.
- `plugin.render_wake_message(events)` — the provider's own header renderer
  (the CLI composes all wake providers into one turn).
- `plugin.get_commands()` — contributes the `/jobs` slash command.
- `plugin.monitor_status()` — short "jobs are running" indicator.

See `examples/bgjobs_wake_host.py` for a standalone host that resumes the
same `ConversationTree` when woken.

## Security

Launching a job is permission-gated (you approve the exact command once), but
the job is **detached**: it keeps running after the run ends and its output is
captured to disk. Treat `run_bg_job` as standing shell access until
`stop_bg_job`. In non-interactive (one-shot) hosts the permission gate fails
closed like any other tool call.

## Development

```bash
pytest tests/phoson_plugin_bgjobs -q
```

Unit tests run against `tmp_path` and real short commands (no network); the
wake path uses `on_wake` + the persistent queue.