# Key bindings (customizable)

The full-screen TUI's keys are remappable from the `[keys]` section of
`~/.phoson/config.toml` — one line per action, each a prompt_toolkit key
sequence (a list means "try in order", and `""` unbinds the action):

```toml
[keys]
toggle_reasoning = "c-x"          # Ctrl+X instead of Ctrl+T
line_up = ["s-up", "c-up"]        # list = precedence order
submit = ""                       # unbind (use mouse / another key)
```

`/keys` lists the effective map (defaults or your remaps) plus the
config syntax. Sequences are validated at startup: an unparseable key,
an unknown action, or a sequence bound to two actions is a clear error
before the UI opens — never a silent fallback. Remaps apply on the next
start. The classic REPL's single global key (Ctrl+T) is fixed.

## Newline: Shift/Ctrl/Alt+Enter

`Ctrl+J` inserts a newline on every terminal. **Shift+Enter**, **Ctrl+Enter**
and **Alt+Enter** do too wherever the terminal can tell them apart from a
plain Enter (they decode to the same key as the `newline` action):

- On start the TUI asks the terminal to report modified keys:
  - the **kitty keyboard protocol** (flag 1, *disambiguate*) — what makes it
    work out of the box in kitty, and also in Ghostty, WezTerm, foot and
    Alacritty. It is enabled only while the TUI owns the alternate screen
    (pushed on entry, popped before leaving), so your shell never inherits
    it. Under it Esc, Ctrl+key and Alt+key arrive as CSI-u sequences; the
    TUI decodes them back to the usual keys, so every binding (including
    `[keys]` remaps) behaves the same;
  - xterm `modifyOtherKeys` level 1 (reset on exit), for xterm and others
    that don't speak the kitty protocol.

  Set `PHOSON_EXTENDED_KEYS=0` to skip both requests.
- CSI-u encodings sent without the protocol (iTerm2's *Report keys using
  CSI u*, a kitty `map shift+enter send_text all \x1b[13;2u`) are recognized.
- Alt+Enter (`ESC` + `CR`) works where Option/Alt sends Meta. If your
  terminal sends a bare Enter for Shift+Enter (e.g. Terminal.app, iTerm2
  and VS Code by default), map Shift+Enter to send `\x1b\r` (ESC + CR) in
  the terminal's key settings.

## Reasoning toggle

Press `Ctrl+T` to toggle the live "thinking" view while a run is
streaming, or to expand the full reasoning of the last turn after it
finishes (persisted with the session, so it survives resume).

## Reasoning effort cycle

Press `Ctrl+E` to cycle the reasoning effort
`off → low → medium → high → xhigh → max → off`. The header shows the
current level (`effort: high`, dim `· effort off` when unset), the value
is persisted like `/reasoning-effort`, and it applies from the *next*
turn — an in-flight run keeps the level it started with. `Ctrl+E` only
changes the *level*; `Ctrl+T` owns the show/hide axis.
