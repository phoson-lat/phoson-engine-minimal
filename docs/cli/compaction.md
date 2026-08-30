# Context management (long sessions)

When a session grows past a fraction of the model's context window,
phoson compacts it automatically — older turns are replaced by a
**structured handoff summary** (goal, completed work, key decisions, a
distillation of the model's reasoning, open questions, next steps,
constraints) so continuity survives long tasks. Captured reasoning from
the summarized turns is folded into that summary, not dropped.

You control it:

- `/compact` previews what would be summarized and asks before applying
  it; `/compact aggressive` previews a deeper cut.
- `/compact on|off` toggles *automatic* compaction at runtime
  (persisted).
- `~/.phoson/config.toml` `[defaults]` knobs: `compact_mode`
  (`balanced`|`aggressive`|`off`), `compact_threshold` (fraction of the
  window that triggers auto-compact), `compact_min_keep_messages` (recent
  turns kept verbatim), and `offload_tool_outputs` / `offload_max_chars`
  (large tool results — default >24 KB — are written to
  `~/.phoson/compacted/` with only a head/tail preview kept in context).
