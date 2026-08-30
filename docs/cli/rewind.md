# Rewind (double-Esc, full-screen TUI)

Press `Esc` twice in quick succession while idle and a picker lists your
earlier messages — select one to jump the conversation back to just
before it, the same UX as Claude Code's double-Esc. The picker lists only
your own messages, newest first. The chat pane redraws up to that point,
your composer is pre-filled with the selected message (edit it and press
Enter to re-send), and `Ctrl+Z` undoes the jump, restoring the previous
point (repeat it to undo several consecutive rewinds).

The "undone" messages are not deleted — they remain as an abandoned
branch in the conversation tree (still visible via `/tree`), and session
cost/token totals stay cumulative (same contract as `/undo`).

Precedence with the single-Esc run cancel is fixed: a lone `Esc` while a
turn is running still cancels it immediately, and double-Esc is only
interpreted when idle (Alt+Backspace and other multi-byte sequences are
never misread as a double-Esc).

The double-tap rides on whatever key `escape` is bound to — remapping
`escape` in `[keys]` moves both the single-Esc cancel and the double-Esc
rewind together (unbinding `escape` disables both); the jump undo
`undo_jump` (default `Ctrl+Z`) is remappable on its own.
