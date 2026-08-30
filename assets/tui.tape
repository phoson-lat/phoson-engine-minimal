# Phoson CLI hero tape — regenerates assets/tui.gif (I-115).
# assets/tui.png (the README hero) is the last frame of tui.gif:
#   ffmpeg -sseof -0.5 -i tui.gif -frames:v 1 tui.png
#
# Shows the full-screen TUI mid-conversation: chat pane with a completed
# user turn, a read_file tool card, the assistant answer, and the idle
# composer.
#
# Prereqs: vhs >= 0.10, ttyd >= 1.7.2 on $PATH, tmux, a working provider
# (a local vLLM at the configured base_url works — this is how the
# committed PNG was produced).
#
# Setup:
#   mkdir -p /tmp/phoson-demo && echo 'hello from phoson' > /tmp/phoson-demo/hello.txt
#
# Render (from the assets/ directory). The env -i + SHELL=/bin/bash
# wrapper keeps the capture clean of the host's login-shell prompt
# (e.g. p10k/zsh):
#   env -i HOME="$HOME" SHELL=/bin/bash TERM=xterm-256color \
#     PATH="$(dirname $(command -v ttyd)):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
#     vhs tui.tape
#   ffmpeg -sseof -0.5 -i tui.gif -frames:v 1 tui.png
#
# Timing: Sleep 16s assumes a sub-~12s model run on the configured
# provider; bump it if your backend is slower.

Output tui.gif

Require uv

Set Shell "bash"
Set FontSize 18
Set Width 1600
Set Height 900
Set Theme "GruvboxDark"
Set TypingSpeed 30ms

Type "cd /tmp/phoson-demo && uv run --project $HOME/Documentos/Phoson/phoson-engine-minimal phoson-cli" Sleep 300ms  Enter
Sleep 6s

Type "Research about phoson-minimal-engine on Github" Sleep 150ms  Enter
# Wait for: user turn, read_file tool card, assistant answer, idle prompt.
Sleep 30s

# Expand the last turn's reasoning view for a richer screenshot.
Ctrl+T
Sleep 1s

# Hold the frame for the still capture.
Sleep 2s
