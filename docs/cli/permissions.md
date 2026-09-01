# Tool permissions

Control what each tool may do via `~/.phoson/permissions.json`:

```json
{
  "levels": { "bash": "ask", "web_search": "deny" },
  "allow_patterns": { "bash": ["git status", "pytest*", "uv *"] }
}
```

Levels: `allow` (run freely), `ask` (confirm every call), `deny`. A
matching allow-pattern runs without asking even under `ask`/`deny` —
handy for safe subcommands. Inspect or change levels at runtime with
`/permissions bash ask` (persisted immediately).

**Scope.** The policy applies to *every* engine the CLI builds, not just
the interactive REPL:

- **Sub-agents** (`agent` / `agents` tools) inherit the same permission
  gate (and `safe_mode`) as the parent, so a `deny`-level tool is refused
  from a sub-agent exactly as it is from the top-level agent.
- **One-shot mode** (`-p` / piped stdin) runs the same Offload →
  Summarizer → Permission chain as the REPL.

Non-interactive contexts (one-shot mode, scripts, sub-agents with no
confirmation service) **fail closed**: an `ask`-level tool is refused
instead of hanging or running without approval.

## Wall-clock budget for non-interactive runs

One-shot runs have no `Esc` to escape a hung command, so they get a hard
wall-clock cap at the *run* level (not the per-tool timeout, which is
deliberately uncapped for interactive use — I-127):

| Variable | Default | Meaning |
|---|---|---|
| `PHOSON_RUN_BUDGET_SECONDS` | `600` | Max seconds for a non-interactive run. `0` disables the budget (unlimited). |

When the budget is hit the run stops cleanly with a clear message and
exit code **124** (plugins and the model client are closed as usual).
Interactive mode is unaffected — `Esc` remains the escape hatch.
