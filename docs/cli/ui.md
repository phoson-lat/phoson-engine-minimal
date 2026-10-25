# UI behavior (full-screen TUI)

The full-screen `prompt_toolkit` front end is the default interactive
experience; it offers a persistent scrollable chat pane, multiline input
(`Ctrl+J` — or Shift/Ctrl/Alt+Enter where the terminal reports them, see [keybindings.md](keybindings.md) — inserts a newline, `Enter` sends), persistent input history
(`~/.phoson/history.txt`, shared with the retained classic REPL), and
`/model`/`/provider`/`/sessions` pickers and bash confirmation as
overlay floats. The multiline composer wraps long pasted lines, takes
only the height it needs (up to five lines), and scrolls internally
after that cap.

The composer stays usable **while a turn is running**:

- `Enter` with a plain message **queues** it (the draft leaves the composer
  so the next one can be written) and it is sent automatically when the
  current turn settles; a notice in the transcript confirms the queue. If the
  turn is cancelled (`Esc`) or fails, the queued messages are returned to the
  composer instead of being lost.
- **Run-safe slash commands run immediately, side by side with the turn**:
  local UI/config (`/theme`, `/warnings`, `/notify`, `/details`, `/keys`,
  `/help`, `/permissions`, `/reasoning-effort`, `/attach`) and read-only info
  (`/status`, `/about`, `/env`, `/cost`, `/tokens`, `/steps`, `/tree`,
  `/agents-md`, `/skills`). Any other slash command — and `!` shell lines —
  are refused with a notice and the draft is kept, because they compete with
  the running turn.
- `Ctrl+V` (clipboard paste), `Ctrl+P` (command palette, restricted to the
  run-safe commands), `Shift+Tab` (permission mode) and `Ctrl+E` (reasoning
  effort) are all live mid-turn.

`Esc` still cancels the active turn.

## Shell mode (`!`)

Typing `!` into an **empty** composer switches it to shell mode (the same
gesture as Claude Code): the `❯` becomes a `!`, the composer border changes
color and slash/`@file` completion is off. `Enter` runs the text as a shell
command — gated by the same bash permission policy as the agent's tool —
and shows the result as a bash card, then returns to the normal composer.
`Backspace` on the empty composer or `Esc` leaves shell mode without
running anything. A `!` typed anywhere else is just a character.

The command's output is **shared with the agent**, but without starting a
turn: it is queued (the footer shows `N shell outputs → next message`) and
attached to the next message you send, one block per command, so the model
sees exactly what you saw. Starting a new session or loading another one
drops the queue. On resume, those blocks replay as bash cards rather than
as part of your message. Typing a `!command` line directly (e.g. recalled
from history) behaves the same way.

Wrapped and `Ctrl+J` continuation lines in the composer are indented to
line up after the `❯`/`!` glyph.

While a run is in flight the chat shows a transient animated activity
line: `Thinking…` with rotating phrases, then `Composing tool…` while
the model streams a tool call, then `Streaming…` / `Running tool…` as
applicable — the line vanishes when the turn settles.

While idle, press `Esc` twice to **rewind** the conversation to an
earlier message (see [rewind.md](rewind.md)).

## Header and footer

The header (top line) carries only durable session facts — `* phoson · title
(id) · cwd · tokens/cost · permission mode · reasoning effort` — and **no
transient state**, so it stays stable while a turn runs. The live agent
activity (`Streaming…`, `Composing tool…`, `Running tool…`, `Thinking Ns`)
is shown exclusively by the transient in-chat activity line described above.

The context gauge reads `used/window pct%`: dim below 70 %, in the warning
tone from 70 %, and in the error tone with a `/compact` nudge from 90 % (it
can exceed 100 % after switching to a model with a smaller window). When the
header does not fit the terminal it is shortened by priority instead of
being clipped: the title is capped, then agents.md, the update hint,
monitors, cwd, cost, session id and attachments are dropped in that order,
and the title is squeezed before it goes. The context gauge, permission mode
and reasoning effort are always kept.

Every timestamp in the transcript uses one local, contextual format: `14:32`
today, `yesterday 14:32`, `Sep 6 14:32` earlier this year and
`Sep 6 2025 14:32` before that.

The footer (bottom line) shows the active **model/provider on the left** and
the contextual key hints on the right; on a terminal too narrow for both it
degrades to a single left-aligned, `·`-separated line.

## Session titles

A new session is named automatically. A cheap heuristic (first line of the
first user message, truncated) is applied immediately so the session lists
are never empty; after the first **completed** turn the controller asks the
model for a short, specific title (`3–6` words) in the background, so the
turn end is never blocked. The user's `/title` always wins, and a title that
was already set for a resumed session is never rewritten.

The **terminal window title** (OSC 2) mirrors the session title too: it
starts as `phoson-cli`, switches to the session title once one exists, and is
prefixed with `*` while the agent is working (e.g. `* Refactor auth`) so a
backgrounded window shows what is happening at a glance. Piped/non-TTY output
is left untouched.

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
