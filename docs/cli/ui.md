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

One-shot mode (`phoson-cli "task"`) is always stdout-only.
