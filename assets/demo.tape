# Phoson CLI demo tape — regenerates assets/demo.gif (I-115).
#
# Shows one-shot mode: the model emits a read_file tool call, then answers.
#
# Prereqs: vhs >= 0.10, ttyd >= 1.7.2 on $PATH, tmux, a working provider
# (a local vLLM at the configured base_url works — this is how the
# committed GIF was produced).
#
# Setup:
#   mkdir -p /tmp/phoson-demo && echo 'hello from phoson' > /tmp/phoson-demo/hello.txt
#
# Render (from the repo root). The env -i + SHELL=/bin/bash wrapper keeps
# the capture clean of the host's login-shell prompt (e.g. p10k/zsh):
#   env -i HOME="$HOME" SHELL=/bin/bash TERM=xterm-256color \
#     PATH="$(dirname $(command -v ttyd)):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
#     vhs assets/demo.tape   # (run from assets/, output lands next to the tape)
#
# Timing: the Sleep 18s below assumes a sub-~12s model run on the
# configured provider; bump it if your backend is slower.

Output demo.gif

Require uv

Set Shell "bash"
Set FontSize 18
Set Width 1180
Set Height 540
Set Theme "GruvboxDark"
Set TypingSpeed 30ms

Type "cd /tmp/phoson-demo && uv run --project $HOME/Documentos/Phoson/phoson-engine-minimal phoson-cli 'What does the file hello.txt contain? Read it first.' -p" Sleep 300ms  Enter
Sleep 18s
Sleep 1s
