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
`/permissions bash ask` (persisted immediately). Non-interactive
contexts (one-shot mode, scripts) fail closed: an `ask`-level tool is
refused instead of hanging.
