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
The text-selection note lives in the full-screen app's key listing
(``/keys``) — the footer itself is a 3-hint contextual bar.

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

One caveat about the full-screen front end: OSC 8 sequences are carried
through prompt_toolkit as zero-width escapes, and prompt_toolkit does not
repaint a zero-width escape whose column is unstyled trailing whitespace
(issue #1651). A link ending a line used to leave its OSC 8 *close*
unwritten, so the terminal kept the hyperlink open and underlined
everything that followed — the "all text is underlined" glitch.
`phoson_cli.hyperlinks.osc8_passthrough` now anchors such a close onto a
figure space (`U+2007`, visually blank) so the sequence stays balanced.
This is an internal rendering detail; it needs no action from the user.

## Rendering safety

Two classes of rendering glitches are prevented in the full-screen TUI:

- **Links that spread over the screen.** The TUI repaints only the cells
  that change, so a repaint could write the start of a link without its end,
  and the terminal would then treat everything written afterwards (footer,
  spinner, other lines) as part of that link. The output layer now closes any
  open link before the cursor moves, a line ends or the frame is flushed
  (`phoson_cli.hyperlinks.guard_hyperlinks`). Links still work; nothing else
  can inherit them.
- **Characters that break lines.** Rich and prompt_toolkit used to disagree
  on the width of some text: emoji with a variation selector (`⚠️`), ZWJ
  sequences (`👨‍👩‍👧`), skin tones and flags, and raw control bytes or escape
  sequences in tool output. Text then shifted, wrapped early or overwrote
  itself. Every rendered segment is now normalized to the width Rich laid it
  out with: escapes and control bytes become spaces, and composite emoji are
  shown as their base character (flags as their country letters, `🇲🇽` →
  `MX`). In the bash preview, a carriage return (progress bars) keeps only
  what was written last, as a terminal would.
