# Phoson CLI hero tape — regenerates assets/tui.gif (T-10, #160).
# assets/tui.png (the README hero still) is the last frame of tui.gif:
#   ffmpeg -sseof -0.5 -i tui.gif -frames:v 1 tui.png
#
# Shows the full-screen TUI mid-conversation: a completed user turn, a
# patch_file tool card with its visible +/− diff (the harness at work,
# not a research dump), the assistant answer, and the idle composer.
#
# Prereqs: vhs >= 0.11, ttyd >= 1.7.2 on $PATH, uv, a working provider
# (a local vLLM at the configured base_url works — the committed PNG was
# produced this way).
#
# Setup:
#   mkdir -p /tmp/phoson-demo && printf '[server]\nhost = 0.0.0.0\nport = 8080\ndebug = false\n' > /tmp/phoson-demo/app.conf
#
# Render (from the assets/ directory). The env -i + SHELL=/bin/bash
# wrapper keeps the capture clean of the host's login-shell prompt
# (e.g. zsh/p10k):
#   env -i HOME="$HOME" SHELL=/bin/bash TERM=xterm-256color \
#     PATH="$(dirname $(command -v ttyd)):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
#     vhs tui.tape
#   ffmpeg -sseof -0.5 -i tui.gif -frames:v 1 tui.png
#
# Timing: Sleep 20s assumes a sub-~10s model run on the configured
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

Type "Update app.conf to use port 9090 and enable debug mode" Sleep 150ms  Enter
# Wait for: user turn, patch_file tool card with its +/− diff, the
# assistant answer, and the idle composer.
Sleep 20s

# Hold the frame for the still capture.
Sleep 2s
