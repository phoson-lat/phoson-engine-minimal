# Mouse, text selection and hyperlinks (full-screen TUI)

## Selecting/copying chat text

**Drag over the chat to select text; it is copied to the system clipboard
when you release the button.** The selection is shown in reverse video and a
`Copied N chars` message under the prompt confirms the copy.

- Dragging past the top or bottom edge of the chat scrolls it, so a
  selection can span more than one screen.
- A double click selects a word, a triple click the whole line.
- A plain click (no drag) never touches the clipboard.
- The selection clears on the next click, on `Esc`, and when the lines it
  covers are laid out again (a resize, a theme change, `/clear`, or new
  output replacing the part of the in-flight answer it covers). Output
  appended below a selection leaves it alone.
- `Esc` with a selection showing only clears it — it does not count as the
  first `Esc` of the double-`Esc` rewind. While a turn is running, `Esc`
  clears the selection *and* cancels the turn: stopping a run is never
  held back by a highlight.

What is copied is the text as rendered: no colors or link escapes, no
trailing padding, lines joined with a newline. Two consequences of copying
rendered lines:

- a paragraph that was soft-wrapped to the pane width is copied as the
  separate lines it is displayed in;
- frame characters of a card or panel (`│`, `╭─`) inside the dragged range
  are copied along with the text.

For a copy without those, use the terminal's own selection (below).

### How the copy reaches the clipboard

The platform's copy tool is used when there is one: `pbcopy` on macOS,
`wl-copy` on Wayland, `xclip` or `xsel` on X11, `clip.exe` on Windows and
WSL. Over SSH — where such a tool would fill the *remote* machine's
clipboard — and whenever no tool is available, the text is sent as an
OSC 52 escape sequence, which your own terminal turns into a clipboard
write. That works in iTerm2 (enable *Settings → General → Selection →
Applications in terminal may access clipboard*), kitty, WezTerm, Ghostty,
Alacritty, foot and Windows Terminal; macOS Terminal.app ignores OSC 52.
Inside tmux, set `set -g set-clipboard on` (or `allow-passthrough on`).
OSC 52 is limited to about 75 KB of text; a larger selection needs a copy
tool.

### The terminal's own selection

Hold **Shift** while dragging to select with the terminal instead: this
tells the *terminal itself* to ignore the app's mouse tracking for that
gesture and fall back to its native selection/copy (GNOME Terminal, iTerm2,
Alacritty, WezTerm, Ghostty, kitty, Windows Terminal; macOS Terminal.app
uses **Fn** — check your terminal's docs if the modifier differs). The
native selection knows nothing about the app's layout: it only covers what
is on screen.

### Turning it off

```toml
mouse_selection = false      # drags over the chat do nothing in-app
```

Or `PHOSON_MOUSE_SELECTION=0`. The wheel keeps scrolling the chat and
Shift+drag keeps selecting natively.

### Why the app handles the drag

The chat pane sets `mouse_support=True` so the scroll wheel is handled by
the app. That is a terminal-level mouse-tracking switch (xterm's DECSET
1000/1002/1003/1006 modes), not something the app can opt out of
selectively: once it is on, the terminal reports a plain click-drag to the
app instead of selecting text (every mouse-aware TUI hits the same
trade-off). So the app implements the selection itself.

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
