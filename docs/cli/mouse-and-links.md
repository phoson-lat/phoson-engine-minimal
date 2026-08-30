# Mouse, text selection and hyperlinks (full-screen TUI)

## Selecting/copying chat text

The chat pane sets `mouse_support=True` so the scroll wheel is handled by
the app — this is a terminal-level mouse-tracking switch (xterm's own
DECSET 1000/1002/1006 modes), not something the app can opt out of
selectively, and turning it on is what makes the terminal stop treating a
plain click-drag as native text selection (every mouse-aware TUI —
Claude Code, Pi, OpenCode — hits the same trade-off).

Hold **Shift** while dragging to select text: this tells the *terminal
itself* to ignore the app's mouse tracking for that gesture and fall back
to its own native selection/copy, unaffected by whatever the app is doing
(works in GNOME Terminal, iTerm2, Alacritty, WezTerm, Ghostty, kitty,
Windows Terminal — check your terminal's docs if the modifier differs).
The footer's `[Shift+Drag] Select text` hint is a reminder of this.

## Clickable hyperlinks

Markdown links in assistant answers render as real OSC 8 terminal
hyperlinks (`ESC ] 8 ; ; URL ESC \`) in both front ends — the same escape
sequence Neovim, tmux and modern editors use. In a terminal that supports
it (kitty, iTerm2, WezTerm, GNOME Terminal, Ghostty, Alacritty, Windows
Terminal, …) the link text becomes clickable, typically with
**Ctrl+click** (check your terminal's docs — the exact modifier follows
the same convention as the `Shift+Drag` text-selection bypass above:
it's the *terminal* that intercepts the gesture, not the app, so it
isn't affected by `mouse_support=True` capturing the rest of the mouse
for the scroll wheel).
