# Startup and resource usage

The CLI defers loading the front ends for help/version commands, Markdown
rendering until a message needs it, and tokenizer BPE tables until the first
token estimate. Token counting is unchanged; its initialization cost moves
from opening an empty session to the first estimate. Resuming a session may
still need that estimate immediately.

`Ctrl+L` releases the deleted transcript's per-block render caches and finished
tool/plugin bookkeeping. It preserves the active turn and pending tool calls.
Turn termination releases pending calls that never produced a completion event.
Clearing the visible transcript does not delete the agent's conversation history.

## Reproduce the startup measurements

```sh
.venv/bin/python scripts/bench_cli_startup.py --compare-head --runs 7
.venv/bin/python scripts/bench_i84_cpu.py --idle 3 --thinking 3 --stream 2
```

The startup benchmark compares the working tree with a temporary extraction of
`HEAD`, using the same interpreter and installed dependencies. It discards a
warm-up process, then reports medians from fresh processes with warm filesystem
and bytecode caches. It does not load user config or make provider calls.
`app` includes imports and construction with a mocked local provider, not the
first terminal paint, network latency, or a packaged binary's extraction time.
RSS is peak process RSS on Linux, not live Python heap size.

Example local results from this optimization (milliseconds; MiB for RSS):

| Probe | Before time | After time | Before peak RSS | After peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Entry-module import | 403 | 133 | 44.8 | 28.8 |
| `--help` | 404 | 140 | 44.9 | 28.8 |
| `--version` | 420 | 160 | 45.2 | 28.8 |
| TUI imports + construction | 660 | 406 | 87.4 | 45.8 |

These are empty-session measurements. Markdown and tokenizer memory will be
allocated once those features are used; this is not a claim of equivalent RAM
savings throughout a conversation. Times and RSS vary by machine.

The headless CPU probe showed zero repaints and no measurable CPU consumption
in its idle phases; active thinking was about 6% of one core and synthetic
streaming about 8%. This is a diagnostic snapshot, not an active-CPU improvement
claim or a CI performance threshold. Optional plugins and background tasks can
change idle behavior. The script's existing mock-client shutdown notice is not
a provider failure.

## Remaining work

Long transcripts still require per-frame block iteration and concatenation
while streaming. A safe frozen-prefix cache needs a transcript mutation
revision that also catches in-place replacements (tool details, plugin cards,
reasoning expansion). Length/first/last-block checks alone can produce stale
output. Tool result history is intentionally retained for `/details`; bounding
it requires an explicit retention policy rather than silently dropping data.
