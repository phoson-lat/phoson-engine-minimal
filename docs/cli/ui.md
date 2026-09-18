# UI behavior (full-screen TUI)

The full-screen `prompt_toolkit` front end is the default interactive
experience; it offers a persistent scrollable chat pane, multiline input
(`Ctrl+J` inserts a newline, `Enter` sends), persistent input history
(`~/.phoson/history.txt`, shared with the retained classic REPL), and
`/model`/`/provider`/`/sessions` pickers and bash confirmation as
overlay floats. The multiline composer wraps long pasted lines, takes
only the height it needs (up to five lines), and scrolls internally
after that cap. If a turn is already running, `Enter` keeps the draft and
shows a warning; press `Esc` to cancel the active turn before sending
it.

While a run is in flight the chat shows a transient animated activity
line: `Thinking…` with rotating phrases, then `Composing tool…` while
the model streams a tool call, then `Streaming…` / `Running tool…` as
applicable — the line vanishes when the turn settles.

While idle, press `Esc` twice to **rewind** the conversation to an
earlier message (see [rewind.md](rewind.md)).

## Session titles

A new session is named automatically. A cheap heuristic (first line of the
first user message, truncated) is applied immediately so the session lists
are never empty; after the first **completed** turn the controller asks the
model for a short, specific title (`3–6` words) in the background, so the
turn end is never blocked. The user's `/title` always wins, and a title that
was already set for a resumed session is never rewritten.

The call is tool-free, cold (`temperature 0.2`), reasoning-disabled
(`think=False`, which OpenRouter maps to its per-request reasoning opt-out)
and capped at 256 output tokens. It tries `title_model`, then
`subagent_model`, then the active model, in order, until one returns a
title — so a cheap model that belongs to a different provider than the
active one degrades to the active model instead of leaving the heuristic in
place. Any error, timeout or empty reply on the last candidate keeps the
heuristic title. The header shows the title next to the short session id
(`title (1a2b3c4d)`), and updates in place once the async title lands. The
session is not created until the first message starts a run: a fresh
controller shows no session (no id, no title) and writes nothing to disk
until then. On exit, an interactive front end prints
`To resume run: phoson-cli --session <id>` (only when a session was
actually started); `--session`/`--resume` accept an id prefix, exactly like
the `/resume` command.

```toml
llm_titles = true            # set false to keep the heuristic title only
title_model = ""             # empty → subagent_model → active model
title_timeout_s = 8.0
```

Or via the env vars `PHOSON_LLM_TITLES`, `PHOSON_TITLE_MODEL` and
`PHOSON_TITLE_TIMEOUT`.

One-shot mode (`phoson-cli "task"`) is always stdout-only.
