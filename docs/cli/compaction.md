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
- The model itself can call the **`compact_context`** tool (see below) to
  compact on its own judgement.
- `~/.phoson/config.toml` `[defaults]` knobs: `compact_mode`
  (`balanced`|`aggressive`|`off`), `compact_threshold` (fraction of the
  window that triggers auto-compact), `compact_min_keep_messages` (recent
  turns kept verbatim), and `offload_tool_outputs` / `offload_max_chars`
  (large tool results — default >24 KB — are written to
  `~/.phoson/compacted/` with only a head/tail preview kept in context).

## Agent-controlled compaction (`compact_context`)

Automatic compaction is a *safety net*: it reacts to the threshold and is
task-unaware. The `compact_context` tool adds a *strategic* opportunity —
the model can compact **between** tasks, or immediately before reading or
processing a large input, rather than being interrupted by the threshold
gate mid-task.

- **Same effect as `/compact` / automatic.** The tool performs the exact same
  structured compaction: tool-pair-safe cut (`safe_cut_index`), structured
  handoff summary (goal / completed / decisions / distilled reasoning / open
  questions / next steps / constraints), captured-reasoning folding, and the
  empty-summary abort. It takes no parameters; the session's configured
  policy applies.
- **Main engine only.** The tool is wired to the *main* engine's tool list
  only; it is not added to the shared tool registry that sub-agents select
  from, so sub-agents never see it regardless of `tools_subagent_allow`.
  This is deliberate: a sub-agent compacting would rewrite the *parent*
  session's history, which is wrong.
- **When the model is told to use it.** The system prompt advertises the tool
  (and a short "when to call it" note) only when it is in the main engine's
  registry.

### What survives a compaction (automatic, manual, or agent-controlled)

- The structured **handoff summary** of the summarized turns.
- The **recent tail** of turns (kept verbatim, per `compact_min_keep_messages`).
- The **system prompt** and **`AGENTS.md`** memory.

### What does not survive verbatim

- The **older turns** that were summarized — they exist only through the
  summary's fields. A verbatim line of reasoning, an exact error message, or a
  user sentence from a summarized turn may be paraphrased or dropped.

### Rule for critical instructions

Because summarized turns are not preserved verbatim, **never rely on
compaction to keep a critical rule.** Instructions that must hold for the
whole session — coding conventions, "always do X", "never do Y", acceptance
criteria — belong in **`AGENTS.md`** or the **system prompt**, both of which
survive every compaction.

### Difference between the three modes

| Mode | Who triggers | When |
|---|---|---|
| **Automatic** | the engine | context crosses `compact_threshold` of the window (safety net) |
| **Manual** | you | `/compact` (preview + confirm), `/compact aggressive` |
| **Agent-controlled** | the model | `compact_context` tool, at a strategic point in the task |

## Tool-call safety

A tool call spans two or three messages (an `assistant` with the `tool_use`,
then a `user` with the `tool_result`, sometimes a third `user` holding an
image). Providers reject a request whose kept history contains a `tool_result`
with no matching `tool_use` in it (HTTP 400), so a compaction cut must never
split a pair. The cut is placed at a **tool-pair boundary** (`safe_cut_index`):
if the recent-tail cut would land on a `tool_result`, the cut backs up to the
matching `tool_use` and keeps the whole pair. This applies to automatic
compaction, the emergency 400 rescue, the manual `/compact` path (and its
preview) alike. A related guard: if the summary call ever returns an *empty*
result, the compaction **aborts** (the original history is kept) instead of
summarizing the middle into nothing; and the internal summary call is made
**tool-free** so the model cannot answer it with a tool call.

## Terminal notification

`notify_on_completion` (default `off`) controls whether a finished run cues
the terminal (see `/notify`): `bell` (BEL) or `desktop` (OSC 9/777 + BEL
fallback). It is TTY-gated, so piped/script output is never polluted.
