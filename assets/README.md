# Assets

Visuals for the [README](../README.md). Generated with
[VHS](https://github.com/charmbracelet/vhs) (v0.10+) so every capture is
reproducible from the committed `.tape` files.

| File | Tape | What it shows |
|------|------|---------------|
| `demo.gif` | [`demo.tape`](demo.tape) | One-shot mode: prompt → `read_file` tool call → final answer |
| `tui.gif` | [`tui.tape`](tui.tape) | Full-screen TUI mid-conversation: a `patch_file` edit card with its `+/−` diff (animated hero, T-10) |
| `tui.png` | (last frame of `tui.gif`) | Still of the same capture, kept as a lightweight alternative |

## Prerequisites

- `vhs` ≥ 0.11 (`go install github.com/charmbracelet/vhs@latest` or a
  release binary)
- `ttyd` ≥ 1.7.2 on `$PATH` (release binary from
  [tsl0922/ttyd](https://github.com/tsl0922/ttyd/releases))
- A working provider configured for `phoson-cli` (the committed visuals
  were produced against a local vLLM)
- `ffmpeg` (to extract `tui.png` from the final frame, optional)

## Regenerate

```bash
mkdir -p /tmp/phoson-demo
echo 'hello from phoson' > /tmp/phoson-demo/hello.txt
printf '[server]\nhost = 0.0.0.0\nport = 8080\ndebug = false\n' > /tmp/phoson-demo/app.conf
cd assets

# The env -i + SHELL=/bin/bash wrapper keeps the capture clean of the
# host's login-shell prompt (e.g. zsh/p10k). Adjust PATH so vhs, ttyd
# and uv are reachable.
export VHS_ENV='env -i HOME="$HOME" SHELL=/bin/bash TERM=xterm-256color PATH="$(dirname $(command -v ttyd)):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"'

eval "$VHS_ENV vhs demo.tape"
eval "$VHS_ENV vhs tui.tape"
ffmpeg -sseof -0.5 -i tui.gif -frames:v 1 tui.png   # optional still
```

The `Sleep` values in the tapes assume a sub-~12 s model run; bump them
if your backend is slower (`tui.tape` uses a 20 s wait for its edit
prompt).
