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
