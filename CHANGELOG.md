# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

## Unreleased

### Features

- **ACI edit/read tool rework (#179, #180)** — `patch_file` now requires an
  *exact, unique* anchor: with `replace_all=False`, an `old_content` that
  occurs more than once is an error listing the matching line numbers and
  **writes nothing** (F-20). A zero-match anchor gets a closest-line hint
  (difflib) and a CRLF/LF note (F-26). `patch_file` reads raw bytes (no
  universal-newline translation) so a CRLF file is no longer silently
  re-encoded to LF, and the anchor's line endings are normalised to the
  file's dominant ending before matching. `read_file` renders `cat -n`
  (1-based line numbers, display-only) with the 50KB cap now applied to
  *ranges* too, and the truncation note names the exact next range to
  request (F-21a, F-22). `list_dir` caps at 500 entries (F-21b/F-22).
  Tool descriptions rewritten for `read_file`/`write_file`/`patch_file`/
  `list_dir`/`bash`/`agent`/`agents`/`web_fetch` (cuándo/cuándo-no/qué
  devuelve, F-25); `web_fetch` now tells the model its content is data,
  not instructions. The system prompt gains three cache-friendly sections:
  **Tool usage** (guidance gated on the tools actually registered),
  **Environment** (git branch + a capped `git status --short` snapshot,
  only in a git work tree), and **Safety** (no destructive git, no
  commits/pushes unless asked, external content is data). No clock or
  counter enters the stable prefix. Measured against H-1 (#139) pending.

## v0.25.1 (2026-09-03)

### Fixes / Security

- **Bash allow-patterns no longer bless compound shell lines (#175, F-03/F-07)**:
  allow-patterns were `fnmatch`-ed against the *entire* command line, so a
  pattern like `git *` (the typical "always allow") also approved
  `git status; rm -rf /`, `git log | sh` and `git $(rm -rf /)` — the pattern
  blessed the first command while the shell ran the rest. Bash patterns now
  only match a **single simple command**: a line containing any separator
  (`;`, `&`/`&&`, `|`/`||`, newline), subshell grouping (`(...)`) or command
  substitution (`` ` ``, `` $( ``) matches no pattern and falls back to the
  tool's configured level (usually `ask`, so a human sees the whole line).
  Quoting is respected (`git commit -m 'a; b'` is one command).
- **Allow-patterns can no longer be steered onto an unintended argument
  (#175/F-07)**: the permission middleware used to fall back to "the first
  string argument in dict order" for tools without an explicit `match_args`
  entry — an order the model controls — so a `write_file` pattern could
  match `content` instead of `path`. Patterns now apply **only** to tools
  with a declared match argument (`bash`→`command`, file tools→`path`,
  `web_search`→`query`, `web_fetch`→`url`); every other tool resolves purely
  by level. Interactive "always allow" grants follow the same rules.
  No `permissions.json` changes — existing rules keep their meaning.
  Docs: `docs/cli/permissions.md`.

## v0.25.0 (2026-09-01)

### Fixes

- **Compaction no longer breaks tool_use/tool_result pairs (#176, F-10/F-11)**:
  auto-compaction cut the history by index (`others[-min_keep_messages:]`)
  without looking at block types, so the kept tail often started on a
  `user` `tool_result` whose `tool_use` had just been summarized —
  Anthropic answered `400 tool_result ... without tool_use`, which is not a
  context-length error, so the emergency-compaction rescue didn't recognize
  it and the turn died. A new `safe_cut_index` helper backs the cut up to a
  tool-pair boundary and is used in all three compaction sites
  (`_compact`, `_emergency_compact`, `build_compaction`) plus the manual
  `/compact` path, so no kept tail ever starts on an orphaned `tool_result`.
  Two related defects fixed in the same path: a successful-but-**empty**
  summary (the model answered with a tool call, because the internal summary
  round trip inherited the run's tool schemas) used to drop the middle of the
  history silently — it now **aborts** the compaction; and the internal
  summary call now goes out **tool-free** (via an injected chat client) so
  the model can't answer it with a tool call. A tool-pairing 400 that does
  still reach the loop now surfaces as an explicit, diagnosable error
  (`tool_result_without_tool_use`) instead of a cryptic 400.

### Features

- **Terminal notification on run completion (#167)**: new
  `notify_on_completion` setting (`off` (default) | `bell` | `desktop`),
  settable via `config.toml`, `PHOSON_NOTIFY_ON_COMPLETION`, or the new
  `/notify` command. When enabled, a finished run emits a cue to the terminal
  so a backgrounded window gets attention: `bell` writes a BEL (`\a`), and
  `desktop` writes OSC 9 / OSC 777 desktop-notification sequences (iTerm2,
  WezTerm, Windows Terminal, Kitty, XTerm) plus a BEL fallback. The writer is
  TTY-gated, so piped/script output is never polluted with control bytes;
  only successful runs notify (errors/cancellations stay silent). Default is
  `off` to preserve the historical silent behaviour — a bell on every coding
  turn would be intrusive, so the user opts in.

- **Agent-controlled compaction — `compact_context` tool (#147, H-9)**: the
  automatic compaction gate is *reactive* — it fires at a fixed fraction of
  the context window, which knows nothing about the task and can interrupt a
  reasoning step mid-subtask. The new `compact_context` tool hands control to
  the agent so it can compact *strategically* — between tasks, or right before
  reading/processing a large input — on top of the threshold gate, which stays
  as the safety net. The tool takes **no arguments** the model sees: it
  performs the exact same structured handoff the automatic path and `/compact`
  use (tool-pair-safe `safe_cut_index` cut, structured Goal/Completed/Decisions
  /Reasoning/Open-questions/Next-steps/Constraints summary, captured-reasoning
  folding, empty-summary abort). It splices the engine's *in-flight* history in
  place and queues a compaction event that the run-end tree rebase consumes —
  identical to a mid-run auto-compaction — so `/tree` and `base_count`
  bookkeeping stay consistent. It is wired only to the **main** engine (never
  the shared registry sub-agents select from, so a sub-agent cannot compact the
  parent's history), is allowed by the default permission policy, and is
  advertised in the system prompt — with a "when to call it" note and the safety
  rule that critical instructions belong in `AGENTS.md` / the system prompt
  (which survive every compaction) — only when the tool is present. `docs/cli/
  compaction.md` documents what survives (summary, recent tail, system prompt +
  AGENTS.md), what does not survive verbatim (summarized older turns), and the
  three modes (automatic / manual / agent-controlled). 11 new unit tests.

## v0.24.2 (2026-09-01)

### Fixes / Security

- **Sub-agents and one-shot now run with the full middleware chain
  (#174, F-01/F-02)**: previously the `agent`/`agents` tools and the
  one-shot (`-p` / piped stdin) path built their `AgentEngine` without the
  REPL's Offload → Summarizer → Permission chain, so a `deny`-level tool
  was still invocable from a sub-agent, `bash` ran with `safe_mode=False`
  and no confirmation, and one-shot runs skipped auto-compaction. The chain
  construction now lives in a shared helper
  (`session_utils.build_middlewares` / `build_summarizer` / `build_offload`):
  sub-agents inherit the parent's middleware gate and a fresh context
  carrying `safe_mode` / `bash_confirmation` / `plugin_ui`; one-shot
  constructs the same chain (permission gate fails closed, since there is
  no confirmation service).
- **One-shot prints an empty string, not `None`, when there is no content**
  (#174, F-02): `print(result.final_content or "")`.

### Features

- **Non-interactive wall-clock budget (#141, H-7)**: new
  `PHOSON_RUN_BUDGET_SECONDS` (default `600`, `0` = unlimited) caps the
  whole *run* in non-interactive mode — one-shot has no `Esc` to escape a
  hung command. On budget the run exits cleanly with code **124** and a
  clear message, closing plugins and the model client as usual. Interactive
  mode is unchanged.

## v0.24.1 (2026-09-01)

### Fixes

- **TUI: bash output no longer leaks raw control codes (F-42, #186)**:
  `_bash_output_body` now strips OSC sequences (window titles such as
  `\x1b]0;title\x07` emitted by `ls --color`, `git`, or scripts) and parses
  the remainder with `Text.from_ansi`, so real colors become Rich styles
  instead of stray `ESC` bytes that prompt_toolkit's `ANSI()` parser would
  render as literal text in the transcript. A truncated CSI (torn stream)
  is kept literally, which is safe.
- **TUI: frozen-prefix line-bounds fingerprint now includes a cache
  generation (F-41, #186)**: `BlockAnsiCache` carries a `generation`
  counter bumped on every `clear()` (resize, `apply_theme`,
  `_reset_transcript`), and it is the first element of the
  `_compute_chat_bounds` fingerprint. Previously `(width, *id(block))`
  alone would miss a cleared+refilled cache, so a theme with
  differently-long escapes could hit stale cached bounds.
- **TUI: "O(visible)" claim made precise (F-44, #186)**: the
  `_compute_chat_bounds` docstring and the v0.24.0 changelog entry now
  state exactly what the incremental bounds buy — the Python `str.find`
  line-bounds loop runs over the in-flight tail only (O(visible) per dirty
  frame); the transcript assembly and prefix copies still happen every
  dirty frame but are C-speed `memcpy`.

## v0.24.0 (2026-09-01)

### Perf

- **Windowed chat pane (T-14, #171)**: the full-screen TUI no longer feeds the
  whole transcript to prompt_toolkit every frame.

  - *Bug: per-frame cost was O(transcript), not O(visible lines).* The chat
    pane is one `FormattedTextControl` whose content was the **entire**
    transcript; on every frame — spinner tick, streaming repaint, scroll, or
    keystroke — ptk re-ran `to_formatted_text(ANSI)`, `tuple()+hash`, and
    `split_lines` over the full fragment list (2–4× per frame). Measured at
    ~500 turns that blocked the event loop for ~0.5–1 s per repaint, so the
    spinner and 10 fps streaming throttle collapsed to <1 effective fps and
    scroll/typing lagged — *only* in long sessions.
  - *Window the pane.* The full transcript is cached once per width as a single
    ANSI string plus its per-line boundaries; `PhosonApp._render_chat` hands
    ptk only the visible slice (`windowed_slice`, O(visible)). Scrolling
    re-slices the cached string rather than re-rendering, and the cursor is
    pinned at `Point(0,0)` so the logical scroll stays unambiguous. Because
    Rich re-asserts each line's SGR state after every newline, slicing at a
    line boundary re-parses to the same visible text.
  - *Scrollbar from the real transcript.* A `ChatScrollbarMargin` draws the
    thumb from the full transcript's `total_lines`/`scroll_top` (the built-in
    would fill the whole bar, since windowed content height equals the
    viewport).
  - *Measured* (`bench/bench_t14_windowing.py`, 1000 turns / ~15 k lines):
    per-frame cost **609 ms → ~0.9 ms** (×640) and stays **flat** across 40→1000
    turns (windowed fragment count constant at 1740 vs 1,036,000 full).
  - *Verifiable on a real session.* `PHOSON_PERF=1` logs the per-frame
    transcript char count and slice time, so the flat cost is checkable live.
  - *Incremental line-bounds.* `render_chat_split` reports the frozen prefix
    length and `PhosonApp._compute_chat_bounds` caches the frozen prefix's
    per-line offsets against a `(cache generation, width, *id(block))`
    fingerprint, re-scanning only the small in-flight tail per dirty frame.
    Precisely: the per-frame *line-bounds build* (the Python `str.find` loop)
    is O(visible) during streaming instead of O(transcript). The transcript
    assembly itself still copies the frozen prefix into the windowed slice
    each dirty frame, but those are C-speed `memcpy`, not the per-line Python
    loop (F-44 — the earlier "O(visible)" claim was overstated).
    The fingerprint includes the ANSI cache's *generation* (bumped on every
    `clear`, e.g. `apply_theme`) so a theme change with differently-long
    escapes cannot hit a stale bounds cache (F-41).
  - *Bash bodies: no raw control codes.* `_bash_output_body` builds each line
    with `Text.from_ansi`, so `ls --color`/`git` output and window-title
    sequences (`\x1b]0;title\x07`) from `!cmd`/`bash` render as styled text
    instead of leaking raw `ESC` bytes that ptk would print literally
    (F-42).

### Fixes

- **Frozen in-chat spinner (regression from the windowing above)**: the
  visible-slice cache only refreshed on `(top, height, total)` changes, but a
  spinner tick repaints the *same* line count at the *same* window position
  with a new glyph — so the cached fragment kept the old glyph and the braille
  spinner appeared frozen. A `_chat_content_epoch` (bumped on every dirty
  re-render) is now part of the slice-cache key, so a tick re-slices the
  re-rendered transcript. Scrolling is unaffected (its `(top, height, total)`
  and epoch are unchanged → still a cheap re-slice). `PHOSON_PERF` now also
  logs `render_ms` (the dirty-frame re-render cost) alongside `slice_ms`.

## v0.23.0 (2026-09-01)

### Features

- **Command palette (Ctrl+P) + `!` shell (T-12, #162)**:
  - *Command palette:* `Ctrl+P` opens a single fuzzy-searchable picker over
    every native **and** plugin slash command (`/model`, `/theme`, `/sessions`,
    …). `↑/↓` + `PageUp/Down` navigate, `enter` runs the selected command
    (empty args, through the normal `/command` path), `esc` closes. It reuses
    the shared fuzzy scorer and Float-hosting scaffolding, so it opens exactly
    like the model/theme/session pickers.
  - *`!` shell:* a line beginning with `!` runs the rest in the user's shell
    instead of sending an agent turn. The command is gated by the same bash
    permission policy the agent's tool uses — allow → runs, ask → the T-6
    confirmation card (Yes / **Always** / No), deny → refused — and the output
    enters the transcript as a normal bash tool card, so it reads identically
    whether the agent or the user ran it. `/details` can re-collapse it like
    any other finished tool call.

## v0.22.0 (2026-09-01)

### Enhancements

- **TUI reasoning collapsed to a muted line (T-3, #153)**: a finished
  turn's scratchpad no longer renders as a large rounded reasoning Panel
  competing with the answer. It collapses to a single dim `▸ thought Ns`
  line (elapsed thinking time, from the first reasoning event). `Ctrl+T`
  after the turn expands the full text *in place* — still with no box —
  one-shot, like the classic REPL. The Panel render is kept for the classic
  REPL, which prints the scratchpad. The live streaming view is unchanged.
- **TUI composer as an object (T-4, #154)**: the composer no longer reads
  like a shell prompt. It is wrapped in a single rounded `Frame` (the same
  chrome the picker floats use); the old two-rule `─`/`—` sandwich around
  the input is gone (one separator); the `❯` stays *inside* the box; and the
  empty composer shows a dim placeholder (`Ask anything · @ files ·
  / commands`) that disappears on the first keystroke. Newline stays
  `Ctrl+J` (documented in the footer) — a portable Shift+Enter is out of
  scope.

## v0.21.0 (2026-09-01)

### Features

- **`/about` — the Phoson wordmark on demand (T-1)**: with the banner out
  of the transcript, the art has a home. `/about` renders the wordmark +
  provider/model/session meta into the pane on demand (via
  `print_renderable`, so both front ends show it) and is listed in `/help`
  under Info.

### Enhancements

- **TUI chrome dried up (T-1 + T-2)**:
  - *Banner removed from the transcript (T-1, #151):* the 17-line Phoson
    wordmark is no longer injected into the chat sink on startup / theme
    change / rewind. The header already carries provider/model/session,
    and the idle pane now shows a one-line empty-state hint
    (`@ files · / commands`) instead. The art stays available via `/about`.
  - *No per-turn "Phoson" label (T-2, #152):* `render_streaming_panel`
    drops the word "Phoson" before every answer — the reply renders as
    bare Markdown (plus the dim reasoning line when shown).
    `render_assistant_label` is removed.
  - *Badge chips → gutters (T-2, #152):* `render_user_turn` (and the
    matching user rows in `render_history`) use a `›` accent gutter
    instead of a filled ` user ` chip; `render_start_line` drops the
    filled ` assistant ` badge (the header already shows the model);
    history assistant turns are bare Markdown with no `Rule` separator,
    so replay reuses the live-turn primitives. The `session history`
    header is a subtle muted label, not a badge.
  - *Background-free badges (T-2, #152):* `badge_user` / `badge_assistant`
    / `badge_history` lose their `on #…` backgrounds in the dark and light
    themes.
  - *Header (T-2, #152):* idle no longer shows "Online" (the permission
    chip already conveys state); the cost figure only appears when > 0;
    and there is no dangling ` | ` separator when the status is empty.

`formatting.py` / `theme.py` are shared, so the classic REPL receives the
same dry chrome. Tests: banner-seeding tests now assert an empty sink +
the empty-state hint; the "Phoson" / "user" / badge assertions assert the
gutter + bare Markdown; the two badge-background theme tests are replaced
by a no-background SGR check; new `/about` tests.

## v0.20.2 (2026-08-31)

### Fix

- **Full-screen model picker silently dropped failed provider listings
  (#150)**: the full-screen host's `pick_model` accepted the `unavailable`
  list (providers whose live listing failed) but never forwarded it to the
  picker, so in the TUI a provider with a failed listing disappeared from
  the list without a ⚠ row — the list looked complete when it was not. The
  classic REPL host already forwarded it; the full-screen host now does the
  same, so failed listings render as the non-navigable
  `⚠ provider — unavailable: error` section both front ends already had.
  Completes the remaining point of #150 (the same-named-models collapse
  was already fixed by the I-113 unified picker).

Tests: `test_command_host_unit.py` (`test_pick_model_renders_unavailable_providers_section`
regression — fails without the fix — and the no-`unavailable` counter-case).

## v0.20.1 (2026-08-31)

### Fix

- **Model name shown in the classic prompt / banner no longer hides the
  `vendor/` prefix**: the classic REPL prompt (`repl.py`) and the welcome
  banner (`_views.py`) displayed the active model with the prefix stripped
  (`claude-opus-4.6`) while `config.toml`, the session's `last_model` and
  the full-screen header all keep the full saved id
  (`anthropic/claude-opus-4.6`). The mismatch made it look as if the model
  picker had persisted a different (bare) name than the one it showed.
  Both display sites now show the full saved id verbatim, so all three
  surfaces agree with what is persisted. The context-window registry
  lookup (`controller.py`) intentionally keeps stripping the prefix — it
  matches registry keys, not a display.

- **`/subagent-model` inline autocomplete now shows the owning provider**:
  the inline dropdown was the only model UI surface without a provider
  column (`/model` autocomplete, `/subagent-model list` and the
  `/subagent-model` modal picker all show it). Sub-agents run on the
  *active* provider's client with no provider switch, so the column is how
  you tell which dropdown rows the active provider actually serves — e.g.
  while on `vllm`, picking an OpenRouter-catalog id otherwise silently
  falls back to the main model at runtime. The completer now labels every
  suggestion with its owning provider as `display_meta` for both
  `/model` and `/subagent-model`.

Tests: `test_views_unit.py` (banner shows the full saved id),
`test_model_cache_and_completer_unit.py` (`/subagent-model` dropdown rows
carry their provider, including the active-provider case).

## v0.20.0 (2026-08-31)

### Feature

- **reasoning-effort header chip + `Ctrl+E` cycle (T-13)**: the
  reasoning-effort knob existed (`/reasoning-effort <level>`) but was
  invisible — the header never showed it and changing it required a slash
  command. Now it follows the T-6 permission-mode pattern.

  - The full-screen header shows the current level in the accent color
    (`effort: high`) or a dim `· effort off` when unset, and repaints the
    moment the value changes (read from the in-memory config, so the cycle
    just invalidates the header cache — no throttle).
  - `Ctrl+E` (mnemonic: *E* = effort; `Ctrl+T` stays the show/hide toggle
    for the reasoning block) cycles `off → low → medium → high → xhigh →
    max → off`, mutates `config.reasoning_effort` and persists it with
    `save_config(only_fields={"reasoning_effort"})`.
  - The value is read per-run when the controller builds each
    `ModelConfig`, so it applies from the **next** turn — an in-flight run
    keeps the level it started with.
  - Table-driven: a new `cycle_reasoning_effort` action in
    `DEFAULT_KEY_BINDINGS` + `KNOWN_KEY_ACTIONS`, so it is remappable from
    the `[keys]` section of `config.toml` and listed in `/keys` with no
    extra code.

### Enhancements

- **`Thinking {n}s` elapsed-seconds label (T-5)**: the *thinking* phase of
  the in-chat activity line now shows the real wait (`Thinking 8s`) — whole
  seconds on the monotonic clock — instead of rotating stock phrases
  ("Pondering the problem… / Chewing on that…"). The counter runs on the
  wall clock and piggybacks the existing activity-tick repaints (no new
  timer, no extra CPU); it re-arms to 0 on each thinking episode (tool
  start / streamed-text freeze), so it measures the current wait, not the
  whole run. `_THINKING_PHRASES`, `_THINKING_PHRASE_TICKS` and the phrase
  index are gone; the braille spinner and the other phase labels are
  untouched. (IMPROVEMENTS-TUI T-5, issue #155)

- **permission-mode chip + bash confirmation card (T-6)**: the header now
  shows the durable permission mode at all times (`ask` in accent /
  `· auto` dim), and `Shift+Tab` cycles it, persisting per-tool policy the
  same way `/permissions` does. Bash commands in `ask` mode resolve through
  a modal card with the command in monospace and **Yes / Always / No**
  actions — `Always` persists a quoted-glob pattern, and Ctrl+C resolves
  to *no* so a cancelled run never hangs mid-confirmation. The card is
  injected through the `ConfirmationService` protocol, so no tool change is
  required and the classic front end keeps its y/N prompt. (IMPROVEMENTS-TUI
  T-6, issue #156)

- **tool cards in the genre's language (T-7)**: tool cards now read as work,
  not generic rows — a per-family glyph (📖 read / 📂 list / 🖼 image /
  ✍ write / 🪄 edit / ⌘ bash / 🔎 search / 🔗 fetch / 📜 doc), a collapsible
  body (toggle with `/details`, collapsed keeps the header + ✓/✗ + duration),
  and `write_file`/`patch_file` show the diff with a `+`/`−` background and a
  **Created** vs **Updated** label. File paths are emitted as real OSC 8
  `file://` links that the full-screen bridge carries through intact.
  (IMPROVEMENTS-TUI T-7, issue #157)

- **`system` theme tier + JSON drop-in themes (T-8)**: a new `system` theme —
  now the **default** — inherits the terminal's own fg/bg (no `on #rrggbb`),
  so it stops fighting user terminals (Gruvbox, Catppuccin, …); the accent
  token drives the spinner/focus. Users can also drop corrected or custom
  themes into `~/.phoson/themes/*.json` (a `base` + token overrides), which
  appear in `/theme`. The old light/dark question (E4) is obsoleted — the
  terminal resolves it. (IMPROVEMENTS-TUI T-8, issue #158)

- **contextual footer + arrow-free scrollbar (T-9)**: the footer shows three
  hints per state (idle / running / picker open) instead of a long fixed
  cheatsheet, and Shift+Drag scrolls while hinting `/keys` + the docs; the
  chat scrollbar is position-only (no arrows). (IMPROVEMENTS-TUI T-9,
  issue #159)

- **precise types on the confirmation services**: `Awaitable` →
  `Coroutine[Any, Any, None]` on the `ConfirmationService` protocol and both
  front-end implementations (the runtime callback is always a coroutine
  fn), and the full-screen sink's `_tool_calls` log now records the
  `AgentToolDoneEvent` explicitly instead of `object`. Pyright reports 0
  errors across `phoson_cli/`.

### Fix

- **`/theme` crash after `/clear` (stale banner reference)**: `/clear`
  emptied the transcript while the app still held a reference to the banner
  block object, so the next `/theme <t>` raised `ValueError` out of the
  command dispatch loop (`Group object … is not in list`). `clear()` now
  forgets the banner (a cleared transcript intentionally has none), and
  `apply_theme` tolerates a missing banner instead of raising — the same
  stale-reference guard the sink's `/details` toggle already used.

Tests: `test_fullscreen_shell_unit.py` (reasoning-effort chip default/set,
full `Ctrl+E` cycle + wrap + persist against a no-op `save_config`, and that
cycling does not flip the `Ctrl+T` visibility toggle),
`test_sink_unit.py` (elapsed-seconds label truncation, counter runs on the
clock not the ticks, timer re-arms per thinking episode via real
`AgentToolStartEvent`/`AgentToolDoneEvent` sequences, and the `/theme`-after-
`/clear` regression), plus the T-6/T-7/T-8/T-9 suites. **1848 passed**
(38 skipped), pyright 0 errors, ruff clean.

---

## v0.19.0 (2026-08-30)

### Feature

- **monitor plugin (I-126, issue #126)**: new official plugin
  `phoson_plugin_monitor` — long-running background monitors that outlive
  the current agent run and re-activate the agent when their condition
  fires.

  - Tools: `register_monitor(name, kind, spec)`, `list_monitors()`,
    `stop_monitor(name)`, with JSON schemas via `@tool` (kind is an enum:
    `interval` | `file` | `command`).
  - Kinds: `interval` (fire after/every N seconds), `file` (poll a path or
    glob for creation / mtime+size change), `command` (run a shell command
    on an interval; fire on non-zero exit, timeout, or changed output;
    timeout kills the whole process group).
  - Wake mechanism: every fire lands in a **persistent queue**
    (`wake.jsonl` under `data_dir`, default `~/.phoson/monitors/`) as the
    source of truth, carrying the original `session_id`; hosts may also
    pass an `on_wake` callback for live re-activation.
  - Persistence: `monitors.json` registry + wake queue survive process
    restarts; the disk is the source of truth and in-memory tasks are
    resurrected by `ensure_started()` (engine rebuilds, `/model`, restarts).
  - CLI integration (opt-in): `enable_monitors = true` in `config.toml`
    (or `PHOSON_ENABLE_MONITORS=1`). The CLI injects a
    `session_id_provider` into the agent context, (re)starts monitors on
    every engine rebuild, and drains pending wakes into the next user turn
    (`[MONITOR EVENTS]` header, announced via the sink). While the agent
    is **idle** a wake loop re-activates the agent on its own: fires
    trigger an autonomous `[MONITOR EVENTS]` turn rendered like any user
    turn; wakes arriving mid-run are folded into the user's next turn
    instead. `/monitors` slash command lists state and pending wakes
    (I-110 extension contract).
  - Status indicator: a neutral `monitor_status()` host hook reports the
    active monitors, surfaced dim in the full-screen header and in the
    classic prompt (`⏳ sensor-umbral, watchdog +2`), so the user can see
    what is watching without asking.
  - Host example: `examples/monitor_wake_host.py` — a standalone embedded
    host that resumes the same `ConversationTree` (`JsonlStorage`) when
    woken. See `phoson_plugin_monitor/README.md` and
    `docs/plans/I-126.md`.

  Tests: storage round-trips/atomicity/corruption tolerance, kinds with an
  injected fake clock (plus real-tick command tests), plugin lifecycle /
  tools / wake path with fakes, an e2e run where a fake LLM registers a
  monitor that fires and the host resumes the same session tree, and CLI
  integration (config opt-in, session provider, drain, rebuild
  resurrection).

### Enhancements

- **monitor plugin decoupling (I-126)**: the in-tree plugins now have zero
  coupling to `phoson_cli` — no imports, no TUI stack, no reverse
  references (verified by tests). `phoson_cli/__init__.py` re-exports
  `PhosonRepl` lazily (PEP 562), so importing the UI-free
  `phoson_cli.config` (what embedded hosts need) no longer drags in
  `prompt_toolkit`. The in-tree plugin fallback for both MCP and monitors
  is now CWD-independent (absolute path) and degrades to a warning instead
  of crashing the engine when the package and the in-tree file are both
  missing.

## v0.18.0 (2026-08-30)

### Feature

- **plugin platform**: community plugins can now extend the CLI without
  modifying `phoson_cli`. The single `Plugin` contract adds optional
  `get_commands()`, `get_tool_render_specs()`, `get_theme_extension()` and
  async `aclose()` hooks, all backed by UI-neutral contracts in
  `phoson_agent.cli_extensions` (I-110, issue #110).

  - `config.toml` supports `plugins = [...]` and loads them alongside MCP in
    classic, fullscreen and one-shot mode. Per-session registries provide
    plugin slash commands in `/help`/completion, tool-card icon+verb, and
    themes derived from the built-ins without global state leakage.
  - `plugin_ui` lets plugin tools/commands publish notices, key/value cards,
    TODO lists and progress; interactive hosts support confirm, select and
    form interactions, while one-shot/CI safely returns `unavailable`.
  - `phoson-cli plugin install|list|enable|disable|remove|update|doctor` and
    `--install-plugin` manage community packages. GitHub/Git sources are
    normalized and pinned to a resolved commit in `~/.phoson/plugins.lock.toml`;
    `--yes` enables intentional automation. Installation validates entry points
    in a fresh interpreter and supports idempotent local development installs.
  - Added `examples/complete_cli_plugin/`, an installable end-to-end example
    with a tool, command, custom card, theme, TODO/progress, selector/form and
    async lifecycle hook. See `docs/plugins.md` and `docs/plans/I-110.md`.

  Tests: plugin contracts, config/one-shot loading, command collision/dispatch
  and completion, scoped render/theme registries, UI adapters, manager/lockfile
  behavior, Git pinning, idempotent installs and a real isolated installation
  smoke test.

## v0.17.1 (2026-08-30)

### Fix

- **cli**: los `warnings.warn` de Python (soft-fails internos) ya no se
  imprimen a stderr como warning crudo con archivo+línea — solo el notice
  estilizado del CLI, una vez (IMPROVEMENTS.md I-112, issue #112).

  Un soft-fail interno (p. ej. el contexto-window "el servidor vLLM no
  lista el modelo", o un model-listing caído) producía **dos** salidas: el
  notice estilizado `⚠ …` **y** el warning crudo de Python
  (`.../context_window.py:NN: UserWarning: …`) a stderr, con rutas
  internas y línea de código. Rompía la TUI y exponía paths.

  - **`phoson_cli/warnings_hook.py`** (nuevo): dos hooks instalados por
    `main()` para toda la run.
    1. `warnings.showwarning` → notice (stdout, nunca stderr; se descarta
       el `filename`/`lineno`, que es lo que expone paths).
    2. Un `logging.Handler` en el root enruta los `logger.warning` de
       `phoson_*` al mismo notice, en vez de que caigan por
       `logging.lastResort` a stderr.
  - **Canal de notice mutable**: el modo clásico apunta el printer a
    `Renderer.print_warn` (theme en vivo; `/theme` lo re-punta) y el
    one-shot se queda con el printer plano (script-friendly, nunca stderr).
  - **Fullscreen**: `run_async` marca `set_fullscreen_active(True/False)`
    alrededor del alt-screen para que los hooks sean no-op (un print fuera
    del buffer rasgaría el render). El par `NullHandler`+`captureWarnings`
    preexistente se conserva intacto.
  - **`phoson_agent/plugins/context_window.py`**: dedup — los 3 except
    (Ollama/OpenRouter/vLLM caídos) emitían `warnings.warn` **y**
    `logger.warning` del mismo fallo; se elimina el `warnings.warn` (se
    queda el log, que es la señal de trazabilidad del issue #23 y que dos
    tests exigen vía `caplog`). El else-branch "modelo no listado" (el
    repro del issue) conserva su único `warnings.warn`.
  - **`phoson_llm/pricing.py`**: se retira el advice obsoleto
    `filterwarnings('ignore', …)` del mensaje de `UnknownModelWarning`
    (el hook gestiona la presentación).

  Tests: `test_i112_stderr_regression.py` (capfd — nada en stderr + notice
  una vez para el repro exacto de vLLM, server-caído dedup, model-listing
  clásico, one-shot, y el hook activo durante `main`/restaurado en
  `sys.exit`) y `test_warnings_hook_unit.py` (mute fullscreen, multi-línea
  → 1 línea, `phoson_*` vs third-party, restore idempotente, printer
  custom). Ver `docs/plans/I-112.md`.

## v0.17.0 (2026-08-29)

### Feature

- **cli**: timeout por invocación en la tool `bash` y en los sub-agents
  (IMPROVEMENTS.md I-127, issue #127).

  La tool `bash` mataba todo comando a los 30 s hardcodeados
  (`DEFAULT_TIMEOUT_SECONDS`) y el LLM no podía subirlo: un
  `pytest`/`pip install`/`docker build` — o un entrenamiento — que
  tardaba >30 s moría con `Command timed out after 30s` y el agente solo
  podía re-ejecutar el mismo comando atascado.

  - **`bash`**: nuevo parámetro `timeout` en el schema del modelo
    (default 30 s, **sin tope máximo** — la libertad para runs largos es
    el requisito; el escape ante un hang es cancelar el run con Esc).
    Invalid values (`<=0`, no numéricos) → fallback a 30 s con una nota
    en el resultado; strings numéricos se aceptan en silencio.
  - **`agent`/`agents`** (extensión acordada): el timeout del sub-agent
    (default 300 s) antes solo se configuraba por
    `config.toml`/`PHOSON_SUBAGENT_TIMEOUT` y era invisible al LLM. Ahora
    ambos wrappers aceptan `timeout` por invocación: omitido → default de
    config (backward compatible), `>0` → ese valor sin tope, `0` → sin
    timeout (semántica preexistente de la config), inválido → default +
    nota. Cierra el loop del entrenamiento: `agent(task=…,
    timeout=14400)` + `bash(timeout=14400)` interno.
  - **`_timeouts.sanitize_timeout()`** (nuevo, compartido): valida los
    overrides del modelo (coerce strings numéricos; rechaza bools,
    negativos, NaN, basura) y rinde `(effective, note)`.
  - **`phoson_agent/tool.py`**: fix — `_json_schema_for_type` perdía la
    descripción `Annotated` en unions opcionales de 1 arg
    (`Annotated[float | None, "…"]` → schema sin descripción); ahora se
    conserva (regresión cubierta en `test_tool_unit.py`).
  - Docs: `docs/api/phoson_cli.md` — sección `BashTool` obsoleta
    (documentaba una clase inexistente) reescrita con la API real;
    `SubAgentTool` documenta las dos capas de timeout (por invocación vs
    config).

  Tests: schema de `bash`/`agent`/`agents` (timeout opcional `number`
    con descripción, `required` intacto, injected sigue oculto), default
    inalterado, override forward, sin tope (14400 respetado), sanitización
    (negativo, 0, NaN, bool, `None`, `"abc"`, `"45"`), e2e reales
    (timeout corto mata el sleep; budget amplio pasa), sub-agent con
    config/override/`0`/inválido + timeout real contra chat lento, y 17
    unit tests del sanitizer. Ver `docs/plans/I-127.md`.

## v0.16.1 (2026-08-29)

### Fix

- **llm**: cargar una sesión con attachments `file://` temporales ya
  borrados (p. ej. `file:///tmp/shot-accepted.png`) deja de crashear la
  conversión de mensajes con `FileNotFoundError`
  (IMPROVEMENTS.md I-119, issue #119).

  Al reabrir una conversación (`--resume`, reintento del summarizer), la
  re-conversión para el próximo `llm.stream()` re-leía el archivo del
  attachment sin chequear que siguiera vivo; los de `/tmp` no
  sobreviven entre runs y la excepción propagaba fuera del stream.
  `load_file_as_base64()` (`phoson_llm/utils.py`) ahora retorna
  `str | None`: archivo ausente o ilegible → `logger.warning` +
  `None` en vez de `open()` sin catcher. Los **seis** sitios que leen
  `file://` (OpenAI-compatible: image + audio — este último usaba un
  `open()` crudo, ahora por el helper compartido; Anthropic: image +
  document; Gemini: image + document) degradan el bloque a texto
  visible vía `missing_attachment_placeholder()`, p. ej.
  `[image no longer available: shot-accepted.png]` — mismo patrón que
  los placeholders de bloques no soportados que ya existían. Fuentes
  vivas (`file://` existente, `data:`, `https://`) no cambian.

  Tests: contrato nuevo (ausente/directorio/ilegible → `None` +
  warning; shape del placeholder) y degradación por adapter, incluido
  el criterio del issue — `_convert_messages` con una imagen muerta:
  sin excepción, nada en stderr, placeholder presente. Ver
  `docs/plans/I-119.md`.

## v0.16.0 (2026-08-29)

### Feature

- **agent/cli**: feedback en vivo mientras el modelo compone la tool call
  (IMPROVEMENTS.md I-128, issue #128).

  El agente ya recibía `ToolCallDeltaEvent` por cada chunk de los
  providers (OpenAI-compatible, Anthropic, Ollama) pero
  `AgentLoop._consume_llm_stream()` los descartaba: en una `write_file`
  de 200 líneas la UI se quedaba muda hasta que la tool empezaba a
  ejecutarse. Ahora se emite el nuevo **`AgentToolComposingEvent`**
  (exportado en `phoson_agent`), con throttle leading-edge de ~250 ms
  (~4 eventos/s): el primer chunk de args y el primer nombre conocido
  siempre se emiten; el resto son heartbeats. `args_chunk` es JSON
  parcial y opaco (nunca parseable); `tool_call_id` viaja vacío
  (solo existe al llegar `AgentToolStartEvent`); providers sin deltas
  (p. ej. Gemini) simplemente no lo emiten.

  - **Full-screen**: `CurrentTurn.composing_tool` alimenta la activity
    line del pane (`⚙ writing file…`), el header muestra
    `Composing tool` y la línea sigue animada durante la generación.
    Al aterrizar la card de `AgentToolStartEvent` el label se limpia
    (reemplazo in-place, sin duplicados ni líneas huérfanas — el estado
    vive en el turno, no en `blocks`).
  - **Clásico**: el spinner se relabelfea a `⚙ {verb}…` en
    `_on_tool_composing()` (idempotente; se ignora si aún no hay nombre
    o si el Live panel de streaming está abierto).
  - Tests: throttle (tracker + demux + engine), orden composing →
    `AgentToolStartEvent`, streams de solo texto/reasoning sin
    composing, error a media composición sin ejecutar la tool, sink
    fullscreen (activity line, header, animación, limpieza) y golden
    ANSI, y spinner clásico (relabel, noop con Live abierto).

## v0.15.0 (2026-08-29)

### Fix

- **cli**: Alt+Backspace (y cualquier tecla Alt-modificada) ya no se
  interpreta como Esc — no cancela un run en vuelo ni abre el picker de
  rewind (IMPROVEMENTS.md I-108, issue #108).

  Los terminales codifican Alt+<tecla> como `ESC` + <tecla> (convención
  Meta). Para Alt+Backspace los bytes son `0x1b 0x7f` y el parser VT100
  de prompt_toolkit los entrega como dos KeyPress — `escape` y `c-h` —
  en el mismo lote; con la binding `eager` de `escape`, el handler
  disparaba para el `ESC` aunque fuera prefijo de la secuencia.
  `PhosonApp._is_prefixed_escape()` asoma a
  `app.key_processor.input_queue` (el resto del lote ya está en la cola
  cuando el handler eager corre) y suprime el Esc solo cuando la
  siguiente tecla tiene `data` en el rango 0x20–0x7F (el payload
  Meta): no confunde con un doble-Esc real (segundo `ESC`, data `0x1b`),
  ni con un Ctrl+C/Enter ajenos en la cola (data < 0x20). Regresión #68
  intacta: un Esc limpio en vuelo sigue cancelando de inmediato.
  Tests: PipeInput con `ESC+0x7f`/`ESC+x` (idle y mid-run) +
  `ESC` solo mid-run + caso unitario de la heurística.

### Fix

- **cli**: el rewind picker lista solo turnos genuinos del usuario, en
  orden nuevo→viejo, con el cursor inicial en el más reciente
  (IMPROVEMENTS.md I-109, issue #109).

  `SessionController.jump_candidates()` ahora recorre el path activo en
  **reversa** (el candidato 1 es el turno de usuario más reciente, el
  más probable objetivo de un rewind) y hace el filtro **consciente del
  contenido**: un nodo role-`user` solo califica si su contenido es
  `str` o contiene al menos un `TextBlock`. Los tool results se guardan
  con role `user` y contenido solo `ToolResultBlock`
  (`phoson_agent/_tool_runner.py`); con el filtro por role solían
  filtrar como filas "(empty message)". Un turno de usuario con string
  vacío/blanco sigue apareciendo (es un turno real).

### Feat

- **cli**: binarios standalone de `phoson-cli` para Linux, macOS y
  Windows sin Python (IMPROVEMENTS.md I-93, issue #93).

  - *Spec.* `phoson_cli.spec` (PyInstaller onefile, entry
    `phoson_cli/__main__`): stages `phos-ascii.txt` bajo
    `phoson_cli/`, recoge hidden imports de los 6 paquetes propios
    (el plugin loader los importa con `importlib.import_module`) y de
    los SDK opcionales de providers (google-genai, mistralai, boto3) y
    plugins (mcp, asyncpg, redis, qdrant) cuando están en el entorno de
    build; `--version X.Y.Z` inyecta `phoson_cli/_frozen_version.txt`
    en el bundle.
  - *CI.* `release-binaries.yml`: matrix de 5 runners (Linux x86_64 y
    ARM64, macOS Apple Silicon e Intel, Windows x86_64); la versión
    viene del tag del release; adjunta los binarios al release con los
    nombres de la tabla del README (`phoson-cli-linux-x86_64`,
    `phoson-cli-darwin-arm64`, `phoson-cli-windows-x86_64.exe`, …).
  - *Runtime congelado.* `phoson_cli/_frozen.py`: `asset_path()`
    resuelve assets en `sys._MEIPASS/phoson_cli/` (bundle) o junto al
    módulo (source); `frozen_version()` lee la versión inyectada en
    build porque un bundle no trae metadata de paquete. El banner
    (`_views.py`, `installer.py`) lo usa.
  - *Updater.* nuevo `InstallMode.FROZEN` (detectado primero);
    `get_current_version()` prefiere la versión inyectada; el hint de
    actualización para el binario apunta a la página de Releases.
  - *Docs.* README: sección "Standalone binaries (no Python required)"
    con la tabla de assets por plataforma.

## v0.13.12 (2026-08-28)

### Feat

- **mcp**: per-server and per-tool enable/disable toggles
  (IMPROVEMENTS.md I-100, issue #100).

  - *Config flags.* `mcps.json` server entries now accept two optional,
    backwards-compatible fields: `"enabled": false` disables the whole
    server (no connection opened at startup, none of its tools exposed)
    and `"tools": { "<remote_tool>": false }` disables single remote
    tools (missing map/entry = enabled).
  - *`/mcp toggle <server> [tool]`.* Flips either flag in `mcps.json`
    (with `.bak` backup) and reloads the engine so it applies in-flight.
    The tool argument accepts the remote name (`read_file`) or the local
    prefixed name the model sees (`mcp_filesystem_read_file`). When MCP
    is globally off the change is persisted with a warning instead of
    an engine rebuild.
  - *Guard rails.* Disabled servers/tools are skipped during discovery
    and rejected at execution time (`ServerDisabled` / `ToolDisabled`)
    so even a stale proxy tool call fails cleanly.
  - *Visibility.* `/mcp status` marks disabled servers and tools with
    `(disabled)`; `/mcp help` lists the new subcommand.
  - *Docs.* `docs/mcp-cli.md` documents the flags and the toggle;
    `mcps.json.example` shows `enabled`.

## v0.13.11 (2026-08-28)

### Fix

- **cli**: `/model <id>` without a `vendor/` prefix now switches the
  provider too (I-113 follow-up). Local servers (vLLM, Ollama, LM
  Studio) serve unprefixed ids like `Qwen3.8-27B-FP8`; the live-listing
  lookup that resolves the real provider was gated on `"/" in id`, so
  switching OpenRouter → vLLM via autocomplete only saved the model
  string and left the backend on OpenRouter. The lookup now always
  runs for an explicit `/model`, preferring a listing that is *not*
  the active provider when several match.

## v0.13.10 (2026-08-28)

### Feat

- **cli**: unified model selection — one picker across every configured
  provider, OpenRouter ordered by `agentic_index`, failed providers
  marked `unavailable` (IMPROVEMENTS.md I-113, issue #113).

  - *OpenRouter ordering.* `/model`, the inline autocomplete and
    `/model list` now sort OpenRouter by
    `benchmarks.artificial_analysis.agentic_index` (descending) — the
    strongest agentic/tool-use models first; models without the field
    are listed last, alphabetically. The current model always stays on
    top. Non-OpenRouter providers keep their exact previous ordering.
  - *Unified picker.* A bare `/model` opens one picker spanning all
    configured providers (fetched **concurrently** via
    `list_models_for_providers`, active provider first). Each row shows
    `id (provider)`; the current *(model, provider)* pair is marked.
    Selecting a model from another provider switches the pair together
    and persists both to `config.toml` (reuses the I-89 path) — no more
    `/provider` → `/model` two-step. Both frontends are parity: classic
    opens the full-screen picker, the full-screen TUI opens the same
    picker as a Float (inline `/model <name>` autocomplete stays, now
    fed by all configured providers, with each suggestion showing its
    owning provider dimmed on the right).
  - *`unavailable` instead of silent fallback.* When a provider's live
    listing fails (timeout, 401, rate limit…), it is shown explicitly
    as `⚠ <provider> — unavailable: <reason>` in the picker and in
    `/model list`. Internally listers now raise `ModelListingError`;
    the single-provider fast path (`/subagent-model`, autocomplete
    cache) keeps its exact old behavior (warning + current-model
    fallback).
  - *Docs.* `docs/api/phoson_cli.md` and `README.md` no longer describe
    the removed on-disk model-listing cache ("instant picker, TTL 24 h,
    works offline"); the live-listing / unavailable / unified-picker
    behavior is documented instead.

### Fix

- **cli**: explicit `/model <id>` (typed or autocompleted) now always
  resolves the *real* provider before switching, instead of silently
  keeping the active one while only the `model` string got saved
  (found while validating I-113 in a live session).

  - A model whose vendor/ prefix isn't itself a provider phoson talks
    to directly (e.g. an OpenRouter catalog entry like
    `qwen/qwen3.8-27b`) never triggered a provider switch at all.
  - Worse, the vendor/-prefix heuristic (`model_provider_for`) is
    *ambiguous* whenever a router re-exports another vendor's catalog
    id verbatim: `anthropic/claude-opus-5` served by OpenRouter is not
    evidence of a directly-configured Anthropic credential. With a
    non-router active provider (e.g. `vllm`), the heuristic confidently
    (and wrongly) resolved to `anthropic`, rejecting the switch with
    *"belongs to provider anthropic, which has no credentials
    configured"* even though the model was legitimately servable via
    OpenRouter.
  - Fix: any explicit `/model <id>` containing `/` now confirms the
    real provider via a live multi-provider listing lookup (the same
    authority the interactive-picker branch already had), falling back
    to the prefix heuristic only when no configured provider's listing
    contains the id at all.
  - `logging.captureWarnings(True)` for the duration of a full-screen
    session: raw `warnings.warn(...)` (e.g. the vLLM/context-window
    "response did not include `<model>`" fallback) bypassed logging
    entirely and corrupted the alt-screen via direct stderr writes; now
    routed through logging (captured by the session's existing
    `NullHandler`) and restored on exit.

### Test

- New/updated unit tests: `agentic_index` ordering (with/without the
  field, ties, current-first), multi-provider aggregation and
  concurrency, `unavailable` marking, unified-picker render + cross-
  provider selection, fullscreen Float hosting and multi-provider
  autocomplete cache.

## v0.13.9 (2026-08-28)

### Perf

- **cli(fullscreen)**: full-screen TUI CPU usage cut while idle and
  streaming (IMPROVEMENTS.md I-84, issue #84).

  - *Bug: the stream-repaint throttle was defeated.* `FullScreenSink.on_event`
    ended with an **unconditional** `_touch()`, so every token
    invalidated the UI regardless of `touch_streaming()`'s 10 fps
    coalescing (measured: ~65 repaints/s sustained instead of ~10).
    Token/reasoning/step-done events now mark `_stream_event` and the
    final touch routes them through the (trailing-timer) throttle; all
    other events keep their immediate invalidation.
  - *Hard redraw floor.* `Application` now runs with
    `min_redraw_interval=0.05` (20 fps cap): redundant invalidations
    coalesce at the prompt_toolkit level. Key *processing* is
    unaffected — scroll/keys still paint on the first available frame,
    so navigation stays fluid (see plan `.opencode/plans/i84-cpu-idle-streaming.md`
    §3bis).
  - *Adaptive activity ticks.* `tick_activity_frame()` no longer
    animates while streaming or a tool/subagent runs — the visible text
    is the feedback there; only the pure-thinking phase keeps the
    spinner alive. The 0.12 s tick cadence is kept unchanged: the first
    pass slowed it to 0.2 s and `min_redraw_interval` to 0.05, which
    made the braille spinner visibly lag (2 s/rotation, ticks deferred)
    — reverted, with the floor lowered to 0.035 s so a spinner tick is
    never deferred to the next frame (test-locked).
  - *Cached header.* `_get_header_text()` builds the HTML string once
    and only rebuilds when an input changes (model, cwd, tokens/cost,
    status, agents-md, update hint) — spinner-glyph repaints no longer
    re-stat the filesystem or reformat the header. Cache is dropped on
    `/theme`.
  - *Measured* (`scripts/bench_i84_cpu.py`, synthetic 60 tok/s burst
    stream, headless TUI): thinking 8.3% → 4.3% CPU, streaming 29.6% →
    4.1% CPU, idle 0% unchanged. New `PHOSON_PERF=1` env var logs
    renders/fps per turn (`phoson.cli.perf`) for before/after evidence.

## v0.13.8 (2026-08-28)

### Feat

- **cli**: model errors now render as a single-line notice that is
  overwritten on each failed retry instead of stacking panels
  (IMPROVEMENTS.md I-83, issue #83).

  - *Panels piled up.* Every `AgentErrorEvent` printed a ~6-line red
    `Panel` with the provider's raw JSON body; each user retry
    ("Continua…") appended another one, so 5 failures consumed 30
    lines of transcript and pushed the conversation off-screen.
  - *Fix: one overwritable line.* New pure helper
    `render_error_notice()` (`formatting.py`):
    `⚠ {code} · retryable — {hint}` for known codes, or a sanitized
    fragment of the message otherwise. The raw body (often raw JSON)
    is never displayed — it is logged at debug level
    (`phoson.cli.errors`) for troubleshooting.
    `render_error_panel()` is kept for expandable/debug views.
  - *Overwrite, don't stack.* `FullScreenSink` tracks the pending
    notice's block index: repeated failures replace it in place (3
    failed retries = 1 line), and the notice is dropped when the next
    run **completes** successfully. `drop_error_notice()` also runs on
    transcript resets (Ctrl+L / rewind re-draws) and self-heals a
    stale index.
  - *Parity.* The classic `Renderer` prints the same notice instead of
    the panel (a real terminal can't rewrite scrollback, so retries
    still add one line each — a line, not a panel).
  - *Tests.* 11 new: notice rendering (hint, fallback, truncation,
    JSON sanitization), sink overwrite/drop/self-heal semantics, and an
    e2e where two failed attempts + a successful retry leave zero
    notice lines in the transcript.

## v0.13.7 (2026-08-28)

### Feat

- **cli**: `/model` now persists the provider alongside the model so
  `config.toml` stays a self-contained, consistent configuration
  (IMPROVEMENTS.md I-89, issue #89).

  - *The pair could rot.* `/model` wrote only the `model` key while
    `/provider` wrote `provider` + `model`, so `/model openai/gpt-4o`
    under an active `anthropic` provider saved a pair that failed on
    the next launch (AnthropicChat built with `openai/gpt-4o`). The
    same mismatch happened when the provider came from an env var:
    the file never learned which provider the model was chosen under.
  - *Fix: model → provider inference.* New pure helper
    `model_provider_for()` (`models.py`): the picker option's
    `provider` field is authoritative; otherwise the `vendor/` prefix
    of the id identifies the provider — except for routers
    (`openrouter`, `github`), which legitimately serve other vendors'
    ids. Unknown prefixes (`qwen/...`, local deployment names) never
    trigger a switch, and aliases (`google`/`gemini`, `aws`/`bedrock`,
    `grok`/`xai`) are normalized.
  - *Runtime + file stay in sync.* `SessionController.set_model(model,
    provider=None)` switches the provider when given, and `/model`
    saves `{model, provider, enabled_providers}` when a switch
    happens, `{model, enabled_providers}` otherwise. When the target
    provider has no credentials configured, the command warns and
    refuses to save the broken pair (runtime untouched).
  - *Bonus:* `/provider` now also refreshes `enabled_providers` in
    narrow saves, so the managed `[defaults]` block no longer drifts
    (issue point 3).

  - *Tests.* 14 new: helper unit cases (option authority, router
    exception, unknown prefix, aliases), controller provider switch,
    command persistence where a restart (`load_config`) reproduces the
    exact `(provider, model)` pair, env-provider file becomes
    self-contained, router keeps its provider, refusal path saves
    nothing, `/provider` keeps `enabled_providers` in sync.

## v0.13.6 (2026-08-28)

### Feat

- **cli/llm**: live cost/tokens in the TUI header + real OpenRouter USD
  cost (IMPROVEMENTS.md I-88, issue #88). Two related gaps in cost
  visibility: the header only updated when a whole turn completed, and
  OpenRouter sessions showed `$0.0000`.

  - *OpenRouter cost was dropped.* The adapter called the shared
    streaming loop with no `cost_calculator`, so every `UsageEvent` was
    `(0.0, cost_known=False)`. But OpenRouter **does** return the
    charged amount in `usage.cost` (and `cost_details`) on the final
    streaming chunk — the OpenAI SDK exposes it as an extra field
    (`extra='allow'`), it just was never read.
  - *Fix: provider cost is authoritative.* `stream_chat_completions`
    now reads `usage.cost` via a new `_extract_provider_cost()` and
    reports it as a known `UsageEvent.cost_usd`, overriding the local
    price table (which stays as the fallback for providers that don't
    report a cost, e.g. OpenAI). Invalid values (non-numeric, negative,
    NaN/inf) are ignored, falling back to the calculator.
  - *Header was not live.* `session_metrics` and `_context_tokens` were
    only updated in `_finalize_run` (end of turn), so the header's
    `tok · $cost` segment jumped at completion.
  - *Fix: live metrics per step.* `_consume_stream` now folds each
    completed `AgentStepDoneEvent` into the session totals via
    `_update_live_metrics()` — cost/tokens accumulate as they happen and
    the context indicator is refreshed against the engine's in-flight
    history (the same conservative estimate the auto-compact gate uses,
    I-91). `_finalize_run` no longer re-adds the steps (that would
    double-count); it only commits the tree and the final indicator.
    The full-screen sink repaints on each `AgentStepDoneEvent`
    (throttled to the streaming cadence) so the header tracks the run.
  - *Bonus:* partial work now survives a failed/cancelled run — steps
    that completed before the failure keep their cost/tokens in the
    session metrics instead of being discarded.

  - *Tests.* 10 new: provider `usage.cost` is authoritative (beats the
    price table), no provider cost → calculator fallback, default
    calculator → cost 0/unknown, invalid cost ignored, OpenRouter E2E
    reports cost > `$0.0000` (and stays unknown without it), controller
    live accumulation with no double count, live context tokens track
    the in-flight history, metrics survive error/cancel, sink repaints
    on step done.

### Fix

- **cli**: `_finalize_run` no longer re-accumulates run steps into
  `session_metrics` (they are now folded in live per step) — prevents
  every step being counted twice.

## v0.13.5 (2026-08-27)

### Fix

- **agent/llm**: auto-compact gate underestimates tokens & no fallback on
  provider 400 (IMPROVEMENTS.md I-91, issue #91). Long sessions could
  die with `HTTP 400: prompt is too long` even though the header showed
  the context "fine" — two independent bugs compounded.

  - *Root cause 1: the gate counted a fraction of the request.*
    `TokenEstimator.count_messages` only scored message text + tool
    args/results; it skipped the **system prompt** (it travels in
    `ModelConfig.system`, not in `messages`), the **tool schemas**
    (sent with every request) and **multimodal blocks** (images/audio/
    video/PDFs were worth 4 tokens of overhead). And the trigger was
    `threshold × window` on the *input* alone — with the default
    `max_tokens=32768` on a 128k window, the provider rejects the
    request at ~95k input while the gate only fired at 102k (80%).
    The gate could never fire in time.
  - *Fix 1: conservative request-level estimate.* New
    `TokenEstimator.estimate_request(messages, system=, tools=)` counts
    messages + system prompt + tool schemas, with flat conservative
    estimates for media blocks (image 1700 / low-detail 1056, audio
    2000, video 8000, PDF 20/page + 1000). The trigger is now
    `min(threshold × window, window − max_tokens − 10% safety)` — it
    fires before the provider can reject. The controller mirrors the
    engine's tool registry into `summarizer.tool_definitions` so the
    gate and the header indicator use the *same* number.
  - *Root cause 2: a 400 context error was terminal.* 400 mapped to
    `code="unknown"`, `retryable=False`, and the run died — no recovery
    path existed.
  - *Fix 2: emergency rescue.* The adapters now classify a 400 whose
    message matches a context-length pattern as
    `code="context_length_exceeded"` (OpenAI-compatible family,
    Anthropic, Ollama, Gemini). In `SummarizationMiddleware`, a
    context-length error **before any user-visible output** triggers an
    emergency compaction and **one** retry: the history front is cut
    until the *summary prompt itself* fits the window (the summary call
    must not hit the same 400), the summary replaces the old middle,
    and the turn continues. If the summary call fails or nothing is
    left to summarize, it degrades to a hard truncation (recent tail +
    notice) — the session survives either way. A second context-length
    error propagates (no retry loops); an error after visible output is
    forwarded as-is (committed response, no duplicate output). The
    rescue also **learns the real window** from the error message
    ("maximum context length is 8192") via a new
    `ContextWindowResolver.override()`, calibrating future gates for
    models the registry doesn't know.
  - *Fix 3: compaction is now persistent.* The old gate built a
    compacted copy for one call and discarded it — the engine's
    history (same list object) kept growing, so every subsequent
    iteration re-fired the gate and paid a *second* summary call.
    Compaction now splices in place (`messages[:] = compacted`), and
    the controller rebases the conversation tree onto the compacted
    history as a new root branch (same semantics as manual `/compact`:
    the old branch stays, visible via `/tree`), announcing the
    compaction to the user. The header indicator
    (`SessionController.estimate_active_path`) uses the conservative
    estimate, so the number you see is the number the gate uses.

  - *Tests.* 30 new: gate fires below 100% with output reserved;
    system+tools counted; media blocks counted; 400 rescue compacts +
    retries once and the session continues; rescue gives up after a
    second 400 (no loops); no rescue after visible output; rescue works
    with `/compact off`; hard-truncation fallback when the summary call
    fails; in-place splice (identity); summary `UsageEvent` cost still
    forwarded; adapter 400 classification (OpenAI-compatible);
    controller tree rebase + header coherence.

## v0.13.4 (2026-08-27)

### Feat

- **cli**: Skills system — on-demand instruction packages
  (IMPROVEMENTS.md G5, issue #52). A *skill* is a directory with a
  `SKILL.md` file (frontmatter `name` + `description`, then Markdown
  instructions, optionally next to bundled `scripts/`/`references/`).
  It is a third abstraction next to the two that already existed:
  plugins are always-loaded engine hooks, tools put a schema in *every*
  request, and skills cost **one line** while dormant.

  - *Progressive disclosure, in two tiers.* `render_skill_index` injects
    only `name: description` per skill into the **stable prefix** of the
    system prompt; `load_skill_body` returns the full body, and it is
    delivered as a **tool result** — i.e. into the conversation, not the
    prefix — so activating a skill on turn 7 cannot invalidate the
    provider's prompt cache (G2). Measured on this repo's own skill:
    **157 tokens indexed vs 2399 loaded (15×)**; discovery costs
    0.43 ms, so it re-runs per call and a skill added mid-session is
    usable on the next turn without a restart.
  - *Activation is a tool, not a slash command or keyword match.*
    Relevance is only knowable after the task is understood, which is
    after the user's message — a slash command would push the decision
    onto the user, and keyword matching fires on false positives ("the
    *architecture* of this function") while missing paraphrases. The
    skill name is an **argument** of the single `skill` tool, so adding
    a skill never changes the tool schemas sent every request (exactly
    the cost skills exist to avoid).
  - *Locations*, first match wins: `.phoson/skills/` (project),
    `.agents/skills/` + `.claude/skills/` (read for compatibility with
    repos already set up for other harnesses — same rationale as the
    `CLAUDE.md` alias in A3), then `~/.phoson/skills/` (global). Project
    skills shadow same-named global ones, mirroring the `AGENTS.md`
    "closer to cwd is more specific" rule; symlinked mirrors
    (`.claude/skills/x -> ../../.agents/skills/x`) are collapsed by
    resolved path so nothing is listed twice.
  - *The `skill` tool only joins the registry when a skill actually
    exists* (`build_tools(include_skill=...)` overrides the
    auto-detection). A schema the model can never use successfully is
    pure prompt cost on every request; for the same reason the prompt
    index is only rendered when the tool is present, so the model is
    never told to call something it does not have.
  - *New `/skills` command* lists what was discovered (with the source
    directory) and `/skills <name>` prints a skill's full instructions;
    inline `/skills <name>` completion in the full-screen composer.
    Bundled resources are listed with the body alongside the skill's
    absolute root, so the model runs them with the existing `bash` /
    `read_file` tools — no new tool needed to make a skill executable.
  - *Frontmatter parsing is dependency-free* (no YAML package): the flat
    `key: value` subset skills use, including folded/indented
    continuation lines for long descriptions. Unknown keys are ignored
    rather than rejected — an extra field must never make a skill
    undiscoverable. Malformed, binary or unreadable `SKILL.md` files are
    logged and skipped, and a failing scan degrades to "no skill tool"
    instead of breaking the registry or the composer.

  Tests: `tests/phoson_cli/test_g5_skills_unit.py` (50 new — frontmatter
  parsing incl. folded lines and unterminated fences, discovery across
  all four locations, project-shadows-global precedence, symlink
  dedup, nested marketplace layouts, `MAX_SKILLS`/description caps,
  unreadable-file resilience, forgiving name lookup with ambiguous
  prefixes rejected, index budget enforcement without mid-entry
  truncation, body loading with resource listing and oversize
  truncation, the `skill` tool's schema and error paths, conditional
  registry wiring, and five system-prompt integration tests asserting
  the index appears, stays byte-stable across turns, and **never**
  carries a skill body).

## v0.13.3 (2026-08-28)

### Feat

- **cli**: clickable OSC 8 hyperlinks in Markdown responses
  (IMPROVEMENTS.md G4, issue #58). Assistant answers with links now
  render as real terminal hyperlinks (`ESC ] 8 ; ; URL ESC \`) in both
  front ends, clickable in terminals that support OSC 8 (kitty, iTerm2,
  WezTerm, GNOME Terminal, Ghostty, Alacritty, Windows Terminal, …) —
  typically with `Ctrl+click`, the same "terminal intercepts the
  gesture, not the app" pattern as `Shift+Drag` text selection (G3).

  - *Root cause.* `formatting.py` passed `hyperlinks=False` to every
    `Markdown(...)` call because prompt_toolkit's `ANSI()` parser only
    understands CSI/SGR (`\x1b[...m`) escapes — an OSC 8 sequence
    (`\x1b]8;;URL\x1b\\`), which doesn't start with `[`, fell through its
    "ignore" branch character by character, leaking raw bytes
    (`8;id=...;https://...`) as literal text around the link.
  - *Fix.* New pure module `phoson_cli/hyperlinks.py::osc8_passthrough`
    wraps every OSC 8 sequence in `\001`/`\002` (SOH/STX) before the
    string reaches `ANSI(...)` — prompt_toolkit's own documented
    mechanism for exactly this case: text between those markers becomes
    a `"[ZeroWidthEscape]"` fragment, which the renderer writes with
    `output.write_raw()`, untouched. Applied inside
    `BlockAnsiCache.get_or_render` (`fullscreen/render.py`), once per
    cached transcript block per width — the in-flight streaming turn
    stays on the plain-text fast path until it freezes into a block, so
    the hot render path is unaffected. The classic REPL prints straight
    to a real `Console` (no `ANSI()` re-parse in between), so it only
    needed `hyperlinks=True` turned back on.
  - *Clicking the link* is a terminal feature, not an app one — the
    terminal intercepts the gesture and opens the URL directly, even
    with `mouse_support=True` capturing the rest of the mouse for the
    chat scroll wheel.

  Tests: `tests/phoson_cli/test_hyperlinks_unit.py` (7 new — OSC 8
  sequence wrapping, no-op on plain ANSI/text, a control case
  reproducing the original bug without the fix, an end-to-end check
  against a real `prompt_toolkit.formatted_text.ANSI()` parse, and the
  full-screen render bridge applying it to a cached block) plus 2
  existing `formatting.py` regression tests flipped to assert OSC 8 is
  now present (they previously asserted its absence, from the
  `hyperlinks=False` fix this release replaces).

## v0.13.2 (2026-08-27)

### Docs

- **cli**: document the `Shift+Drag` native-selection bypass for chat
  text (IMPROVEMENTS.md G3, issue #57), instead of building a dedicated
  copy mode. The chat pane needs `mouse_support=True` for the scroll
  wheel, which is a terminal-level mouse-tracking switch (xterm DECSET
  1000/1002/1006) — once it's on, the terminal reports every mouse event
  to the app instead of handling click-drag as native selection, and
  there is no way to keep the wheel app-driven while leaving drag native.
  A prototype dedicated copy mode (keyboard range-select + mouse
  drag-to-copy + OSC 52 SSH fallback) was built and then dropped after
  comparing it against Claude Code, Pi and OpenCode: all three
  reimplement the same drag-to-copy pattern to work around this same
  trade-off, and OpenCode's issue tracker shows that pattern growing its
  own bugs (clipboard clobbered by incidental selection, mouse capture
  stuck across SSH/tmux/VS Code terminal restarts). The universal
  bypass is a *terminal* feature, not an app one: holding `Shift` while
  dragging tells the terminal to ignore the app's mouse tracking for
  that gesture and fall back to its own native selection (GNOME
  Terminal, iTerm2, Alacritty, WezTerm, Ghostty, kitty, Windows
  Terminal). Added `[Shift+Drag] Select text` to the full-screen footer
  and a README section explaining the root cause — no new interaction
  code, no new bug surface.

### Fix

- **llm**: send `session_id` and `cache_control` inside `extra_body` for
  OpenRouter (follow-up to G2, issue #69). The OpenAI SDK's
  `chat.completions.create()` only recognizes the fields it declares as
  top-level kwargs — both were being passed as bare top-level keys and
  silently dropped en route instead of reaching the request payload,
  which meant OpenRouter's sticky routing and Anthropic automatic
  caching were not actually enabled from the CLI. Both now go through a
  typed `extra_body` dict.

## v0.13.1 (2026-08-26)

### Feat

- **llm/cli**: prompt caching for Anthropic and OpenRouter
  (IMPROVEMENTS.md G2, issue #69). Long agent sessions re-send the whole
  history every turn; this release keeps that prefix cacheable and shows
  the saved tokens:

  - *Stable prefix.* The CLI system prompt is the cacheable prefix of
    every request, but it carried `Current time is YYYY-MM-DD HH:MM:SS` —
    a value that changed on every request and would have busted any
    provider cache for the entire prefix. It now carries
    `Current date is YYYY-MM-DD` (system timezone, B1 behaviour intact):
    constant for a working day, enough for "today" reasoning; the model
    can run `date` via bash for the exact wall clock.
  - *Anthropic.* Explicit prompt caching with three ephemeral
    `cache_control` breakpoints (5-minute TTL; the 1-hour TTL doubles the
    cache-write price and buys nothing for turn-by-turn agentic traffic):
    the system prompt (now sent as a cached block list), the last tool
    definition, and the last block of the last message — which advances
    as the conversation grows, so each turn re-reads the entire prior
    history from cache instead of re-billing it. `tool_use` blocks are
    skipped (the API rejects the marker there); `tool_result` is a valid
    anchor, which matters because in a ReAct loop the last message is
    usually a user turn of tool results. Cache usage
    (`cache_creation`/`cache_read` tokens) was already parsed; the
    adapter just never enabled caching, so every request paid full input
    prices.
  - *OpenRouter.* `ModelConfig` gains `session_id`; the adapter forwards
    it as the top-level `session_id` body field — OpenRouter's
    sticky-routing key, so a conversation stays pinned to one upstream
    provider and its cache is warm from the first turn. `anthropic/*`
    models additionally send the top-level
    `cache_control: {"type": "ephemeral"}` field (automatic caching;
    OpenRouter translates it for Bedrock/Vertex routes); implicit-cache
    models (OpenAI, DeepSeek, Gemini 2.5+) need no flag. The shared
    OpenAI-compatible loop now also parses
    `prompt_tokens_details.cache_write_tokens`, and the OpenAI cost
    callback passes it to `calculate_cost` (GPT-5.6+ explicit
    cache-write pricing). The adapter now attributes its usage as
    *phoson-cli* by default (`HTTP-Referer: https://phoson.lat`,
    `X-OpenRouter-Title: phoson-cli`, `X-OpenRouter-Categories:
    cli-agent`; referer/title overridable as before).
  - *CLI wiring.* `SessionController.run_turn` passes the conversation's
    session id through `ModelConfig`. `/status` gains a
    `cache  R read / W write` line and `/tokens` appends
    `cache=Rr/Ww` (the totals were already accumulated in
    `SessionMetrics`; they are now visible).

  Expected effect: 50–90% lower cost on the repeated prefix plus lower
  TTFT in long sessions.

  - *Docs:* README Features row + "Prompt caching" CLI section;
    `docs/api/phoson_llm.md` (new Prompt Caching section,
    `ModelConfig.session_id`, `TokenUsage` cache fields, adapter notes);
    IMPROVEMENTS.md G2 marked done (v0.13.1); version bumped.

  Tests: 27 new — `test_anthropic_caching_unit.py` (16: breakpoint
  placement on system/tools/last message, `tool_use` fallback,
  `tool_result` anchor, no-anchor edge cases, three-breakpoint budget on
  a full request, cache tokens on the `UsageEvent`),
  `test_openrouter_unit.py` (5: default + overridden attribution headers,
  `session_id` forwarding, `cache_control` on for anthropic / off
  otherwise), `test_openai_compatible_stream.py` (1: `cache_write_tokens`
  parsing; cost-callback test updated to the new contract),
  `test_controller_unit.py` (1: session id → `ModelConfig`),
  `test_p1_commands_unit.py` (2: `/tokens` suffix on/off; `/status` now
  asserts the cache line), `test_session_utils_unit.py` (2: date not
  live clock, byte-identical rebuild).
  Suite now 1351 passing, pyright 0 errors, ruff clean.

### Refactor

- **cli**: the system prompt now carries the date instead of a live
  clock — required for prompt caching (see Feat above); the B1 timezone
  behaviour is unchanged.

## v0.13.0 (2026-08-26)

### Feat

- **cli**: double-Esc rewind to an earlier message (IMPROVEMENTS.md G1,
  issue #51). Press `Esc` twice in quick succession while idle and a
  picker lists the user turns of the active conversation path; selecting
  one jumps the conversation back to *just before* that message — the
  same UX as Claude Code's double-Esc. The full-screen chat pane
  redraws up to the chosen point, the composer is pre-filled with the
  selected message (edit and Enter to re-send), and `Ctrl+Z` undoes the
  jump, restoring the previous point.

  - *Controller.* `SessionController` gains the rewind primitive set —
    `jump_candidates()` (user turns on the active path, oldest first, as
    `(node_id, preview)` pairs), `jump_to_user_turn()` (land the cursor
    on the node *before* a selected user turn) and `jump_to_node()`
    (move to any tree node — what `undo_jump` uses to move forward
    again). All three are generalizations of `undo_last_turn`, which now
    shares the new `_node_path()` helper. The "undone" messages are not
    deleted — they remain in the tree as an abandoned branch (visible
    via `/tree`), and session cost/token metrics stay cumulative
    (intentionally not rolled back, the same contract as `/undo`).
  - *Picker.* New `phoson_cli/rewind_picker.py` — a paged
    `BasePicker` (same scaffolding as the session picker) hosted as a
    modal Float in the TUI; the classic front end reuses it via
    `run()`.
  - *TUI.* `PhosonApp`: double-tap detection inside `handle_escape`
    (1.0 s window over the monotonic clock) — a native `"escape
    escape"` chord can never work here because the single `escape`
    binding is registered `eager` (that eagerness is what keeps the
    single-Esc run cancel of #68 immediate, and it consumes each press).
    The window is deliberately larger than prompt_toolkit's
    `ttimeoutlen` (0.5 s): the VT100 input layer delays delivery of a
    lone Esc by `ttimeoutlen` to disambiguate it from the start of an
    escape sequence, so the *delivered* gap between two idle Escs
    clamps to ~0.5 s and a 0.5 s window would miss real double-taps.
    Precedence is therefore explicit: in flight, `Esc` cancels the run
    and records no double-tap state; idle, a second `Esc` within the
    window opens the picker. On rewind the app rebuilds the transcript
    from the tree (`_reset_transcript()` drops the blocks and the ANSI
    block cache, re-seeds the banner, then `print_history()` replays the
    new path — the same mechanism `/resume` uses), refreshes the header
    token estimate, pre-fills the composer with the selected turn's
    text, and pushes the previous cursor onto a rewind stack.
    `undo_jump()` (`Ctrl+Z`) pops that stack in reverse order and
    redraws forward; consecutive rewinds stack, so repeated `Ctrl+Z`
    walks back through each of them.
  - *Key bindings (E6 table).* New `undo_jump` action (default `Ctrl+Z`,
    remappable; the composer buffer ignores `Ctrl+Z`, so the key is
    free). The double-tap is deliberately **not** its own action: it
    rides on the `escape` action, so remapping `escape` moves the
    single-Esc cancel and the double-Esc rewind together, and
    `escape = ""` disables both. Footer hint adds `[Esc Esc] Rewind`.
  - *Docs:* README "Rewind (double-Esc)" + the UI paragraph;
    `docs/api/phoson_cli.md` key-bindings and rewind sections;
    IMPROVEMENTS.md G1 marked done (v0.13.0); version bumped.

  Tests: `tests/phoson_cli/test_g1_rewind_unit.py` (26 new) —
  controller primitives (candidate list/preview, landing before the
  selected turn, off-path/non-user/root rejections, forward restore via
  `jump_to_node`, unknown id, re-sending branches from the landing
  point), key-map wiring (default `undo_jump`, no phantom `rewind`
  action or chord, remap like any action, `rewind` rejected as an
  unknown `[keys]` action), double-tap state (lone Esc arms without
  side effects, second Esc within the window opens the picker, stale
  press just re-arms, in-flight Esc still cancels and never rewinds,
  rewind ignored while a float is open), apply/undo (redraw drops the
  abandoned branch, composer pre-fill, token estimate, bad node →
  notice only, `undo_jump` restore, stacked rewinds unwind in reverse,
  `undo_jump` without a rewind is one notice, `_reset_transcript`
  re-seeds the banner and drops the block cache), and real-keystroke
  PipeInput e2e (Esc Esc idle opens the picker exactly once, a lone
  idle Esc does nothing, `Ctrl+Z` without a rewind notifies).
  Suite now 1324 passing, pyright 0 errors, ruff clean.

## v0.12.6 (2026-08-26)

### Feat

- **cli**: customizable key bindings for the full-screen TUI
  (IMPROVEMENTS.md E6). The TUI's global key map is now built from a
  table (`fullscreen/keys.DEFAULT_KEY_BINDINGS`) with user overrides from
  a new `[keys]` section in `~/.phoson/config.toml`:

  ```toml
  [keys]
  toggle_reasoning = "c-x"          # single sequence
  line_up = ["s-up", "c-up"]        # list = precedence order
  submit = ""                       # unbind an action
  ```

  - *Core.* `fullscreen/keys.py`: `DEFAULT_KEY_BINDINGS`
    (`{action: [sequences]}` preserving the historical precedence),
    `resolve_key_bindings()` (defaults + overrides merge with
    **cross-action conflict detection** — a sequence bound to two
    actions is an error, never a silent steal),
    `build_key_bindings(app, overrides)` (chords like `"c-x c-e"`,
    `escape` stays `eager`), and `listing_for_config()` (display rows
    for `/keys`; unbound actions show `(off)`).
  - *Config.* `PhosonConfig.key_bindings` +
    `config.load_key_bindings()`: hard validation — unknown action,
    wrong type, unparseable sequence (checked via prompt_toolkit's own
    parser), or an empty list raise `PhosonKeyBindingsError` (a
    `PhosonConfigError`); `main()` prints it and exits 1 with a friendly
    one-line message (no traceback) in every mode. The `[keys]` section
    is user-managed (like `permissions.json`): `save_config` never
    writes it, so a stale managed value can never shadow a hand-edited
    table. `KNOWN_KEY_ACTIONS` is the canonical action list; the classic
    REPL's single Ctrl+T binding is unchanged.
  - *Command.* `/keys` lists the effective map (defaults or remaps) in
    both front ends plus the config syntax and validation rules
    (category "Config & System" in `/help`).
  - *Docs:* README "Key bindings (customizable)" + `/keys` in the
    command list; `docs/api/phoson_cli.md` key-bindings section;
    IMPROVEMENTS.md E6 marked done (v0.12.6); version bumped.

  Tests: `tests/phoson_cli/test_e6_keybindings_unit.py` (43 new) —
  default map shape (covers `KNOWN_KEY_ACTIONS`, identical to the
  historical hardcoded set, all sequences parseable), merge (copy
  without mutation, override, unbind, unknown action ignored,
  cross-action conflicts rejected, self-remap allowed, two overrides on
  one sequence), TUI wiring (full default map, `escape` stays eager,
  remap moves the binding, unbind disappears, chord, conflict →
  `PhosonKeyBindingsError` at construction, escape remap keeps eager),
  display (PgUp/Ctrl+T/chords/`(off)`, default order), config layer
  (no file, no section, string/list/chord, unbind, empty table →
  defaults, unknown action, bad sequence, wrong type, non-string entry,
  empty list, `load_config` integration, `save_config` preserves the
  user `[keys]` section, not a managed key), `main()` friendly failure
  (exit 1, no traceback), and `/keys` (registration, help category,
  effective-map output, dispatchable in both front ends).
  Suite now 1297 passing, pyright 0 errors, ruff clean.

## v0.12.5 (2026-08-26)

### Feat

- **cli**: non-blocking startup update check (IMPROVEMENTS.md E5).
  The CLI now checks PyPI in the background at startup — at most one
  round trip per day — and, when a newer release exists, shows a dim
  one-line hint in the UI: `⬆ v0.8.1 available — /update`. It never
  blocks first paint, input, or a run.
  - *Core.* `phoson_cli.updater` gains `check_for_startup_update()` +
    `startup_check_due()` + `update_hint()`: a JSON cache at
    `~/.phoson/last_update_check` (`PHOSON_HOME` overridable) holding
    `{checked_at, ok, latest_version}`. The check is due when the cache
    is missing/corrupt, older than 24 h, **or the last attempt did not
    succeed** (`ok` false) — failures reset the interval so an offline
    user is retried on the next start instead of waiting a full day,
    while a successful check — including "no update available" —
    sleeps for the whole interval. The fetch reuses `get_latest_version`
    (10 s timeout) and `is_update_available` (so a `dev` source checkout
    still gets the accurate hint); the cache write is atomic (tmp +
    `os.replace`) and best-effort (a read-only home just means no cache).
    Any failure degrades to "no hint": no banner, no message, no retry
    loop.
  - *Classic REPL.* `PhosonRepl.start_update_check()` launches the
    check as a background task from `run()`; the result lands in
    `self.update_hint` and the prompt line renders it as its own dim
    `class:prompt.update` fragment after the arrow:
    `phoson [model·node·12.4k/128.0k] › ⬆ v0.8.1 available — /update`.
    `shutdown()` cancels the task if it is still in flight on exit.
  - *Full-screen TUI.* `PhosonApp.run_async()` starts the same check on
    the shared REPL (single source of truth for both front ends) with
    `on_settle=self.app.invalidate`, so the compact header repaints the
    moment the check lands — even on a fully idle screen. The hint
    renders as a dim `header_dim` segment at the very end of the header
    line; the lower line stays keyboard hints only.
  - *Themes.* `prompt.update` reuses the dim `prompt_tokens` style — no
    new color token, visible but unobtrusive in all four tiers
    (blanked under `no-color` like every other prompt style).
  - Docs: README "Startup update check" note; IMPROVEMENTS.md E5 marked
    done (v0.12.5); version bumped.

  Tests: `tests/phoson_cli/test_e5_update_check_unit.py` (30 new) —
  cache path (default + `PHOSON_HOME`), cadence gating (missing,
  corrupt, wrong shape, no `checked_at`, stale, exact boundary, recent
  failure → retry, recent success → 24 h), hint text, full check flow
  (cache written with the version, no-newer → null, dev install,
  offline → retry next start, not due → zero fetch, 10 s timeout,
  non-raising write on a read-only home), classic wiring (prompt
  fragment with/without hint and fragment order, `start_update_check` →
  hint, failure → None, `on_settle` callback, cancel on shutdown), TUI
  wiring (header with/without hint, `run_async` starts the check,
  offline → no hint), and `prompt.update` in `build_prompt_style`.
  Suite now 1254 passing, pyright 0 errors, ruff clean.

## v0.12.4 (2026-08-25)

### Feat

- **cli**: interactive themes and light/dark auto-detection
  (IMPROVEMENTS.md E4). Two independent pieces: a UI-independent
  terminal-detection layer and a shared live-preview theme picker.
  - *Detection.* New UI-independent `phoson_cli.terminal_theme`:
    `detect_terminal_theme()` → `True` (light) / `False` (dark) /
    `None` (unclassifiable). Order: `COLORFGBG` env (16-color
    `fg;bg` indexes or tmux-style `light`/`dark` words), then an
    **OSC 11** query (`\x1b]11;?\x07`) answered with an sRGB color by
    most modern terminals (iTerm2, kitty, WezTerm, Alacritty,
    ghostty, VS Code, …). The probe is best-effort and never raises:
    raw mode only for its duration (canonical mode would line-buffer
    the newline-less reply), a ~150 ms timeout, and optional
    `termios`/`tty` (non-POSIX → `None`). Classification by WCAG
    relative luminance with a 0.5 threshold. IO is injectable
    (`tty_fd`/`write`/`read`) for TTY-free tests.
  - *First-run suggestion.* `theme.suggest_theme` +
    `__main__._maybe_offer_theme_suggestion`: only when the user has
    never set a theme (no `PHOSON_THEME`, no `theme` in config.toml —
    `config.has_persisted_theme`) and no `--theme` flag was passed for
    this run. When detection resolves it asks one line `[Y/n]` and
    persists via `save_config(only_fields={"theme"})`; when the
    terminal can't be classified (or no-color is forced) it simply
    doesn't ask. Runs **before** the front end is built, so a
    confirmed theme colors the banner on this very startup. Fires at
    most once (the answer is persisted).
  - *Picker.* `phoson_cli/theme_picker.py`: a
    `BasePicker[ThemePickerResult]` structurally identical to the
    model/provider/session pickers. One row per tier
    (`dark`/`light`/`ansi`/`no-color`) plus a **live preview** of the
    selected tier — the banner (art + wordmark) and a swatch strip of
    the named tokens, both rendered in the *previewed* tier's own
    colors (Rich → ANSI → `to_formatted_text`) so it is WYSIWYG even
    though the frame chrome keeps the active theme's palette. Marks
    `(current)` and `(detected)` rows. `build_theme_picker` +
    `pick_theme`.
  - *Wiring.* `CommandHandler._cmd_theme` (new `/theme` spec in
    `COMMAND_SPECS` + `HELP_CATEGORIES`) opens the picker via
    `host.pick_theme` (or `list` / an explicit tier), resolves with
    `theme.get_theme` (direct lookup — env overrides deliberately
    ignored so `/theme ansi` works even under `NO_COLOR`), persists
    and calls `host.apply_theme`. The `CommandHost` protocol gains
    `pick_theme` + `apply_theme`; both hosts implement them
    (`RendererCommandHost` and `FullScreenCommandHost`, the latter
    hosting the picker as a Float).
  - *Apply at runtime* (no restart): `PhosonRepl.apply_theme`
    re-points `self.theme`, `renderer.theme` and the subagent
    spinner; the classic prompt re-applies `build_prompt_style` on
    every `prompt_async` pass. `PhosonApp.apply_theme` extends that
    with the TUI's own consumers: `sink.theme`, the banner
    re-rendered in place, `_apply_style` (the prompt_toolkit style
    dict — chat pane, header, composer, float frames) and a
    `BlockAnsiCache` reset so the chat pane repaints cleanly.
    `StaticArgCompleter("/theme ", …)` autocompletes the four tiers.

### Test

- **cli**: 86 new tests in
  `tests/phoson_cli/test_e4_themes_unit.py` — `COLORFGBG` parsing
  (16-color indexes, tmux words, garbage), OSC 11 response parsing
  (rgb/#hex/decimal, BEL/ST terminators, interleaved escape noise,
  negatives), `query_terminal_bg_light` (injected IO, non-TTY,
  OSError, no response, nonexistent-fd regression),
  `detect_terminal_theme` layering,
  `suggest_theme`, `get_theme`, `has_persisted_theme`, the picker
  (initial row, detected marker, navigation, Enter/Esc, wrap at the
  ends, preview of the right tier, escapes parsed — no raw SGR in
  fragments, no-color plain, Float plumbing), the `/theme` command
  (picker, cancel, explicit arg, unknown name, list),
  `PhosonRepl.apply_theme`, `PhosonApp.apply_theme`,
  `FullScreenCommandHost.pick_theme`, the first-run suggestion
  (flag given, already persisted, unknown terminal, accept/decline,
  EOF), the setup wizard's theme question (detection-driven default,
  empty → default, unknown → fallback), and E2E through `PhosonApp`
  (explicit arg re-colors app + persists + re-renders banner,
  picker confirm re-colors, picker cancel changes nothing). Suite
  now 1224 passing, pyright 0 errors, ruff clean.

## v0.12.3 (2026-08-25)

### Feat

- **cli**: `@file` mentions and path autocomplete (IMPROVEMENTS.md E3).
  The standard `@`-mention pattern (Cursor / Claude Code): typing `@` in
  the composer offers repo paths — filtered fuzzy as you type — and
  sending the message expands each `@mention` into the file's content so
  the model sees the actual file, not just its name.
  - *Core.* New UI-independent `phoson_cli.file_mentions`: `@mention`
    token parsing (ignores `user@domain` emails and bare `@user`
    handles), resolution (relative → cwd, `~/` → home, absolute as-is),
    and content-block building — inlined (head/tail-capped) text for
    code/data files, native media blocks for images/audio/video/pdf
    (same blocks `/attach` builds). Per-message cap (10) and per-file
    size cap (20 MB, matching `view_image`) guard against runaway
    fan-out.
  - *Completer.* `PathCompleter` (in the shared `commands.py`, so the
    classic REPL and the full-screen TUI both offer it) walks the
    working tree once, lazily, on the first `@`, skipping
    `.git`/`node_modules`/… and capping depth (6) and entries (2000) so
    a large repo never blocks the input. Files show a size hint
    (`14 B` / `2.0 KB` / `1.2 MB`).
  - *Controller.* `SessionController._build_user_message` expands the
    mentions into the user `Message` (raw text kept, so `@mention`
    stays visible in context, followed by the resolved blocks) and
    reports each one through the sink: one "Attached: …" info line plus
    a warning per broken `@path` reference. Bare unresolved tokens are
    left as text without noise. Works for every front end (full-screen,
    classic, tests); one-shot is unchanged (no mentions there).
  - *Front ends.* Full-screen app wires `PathCompleter` into the
    `merge_completers` list; the classic REPL merges it with the
    existing `SlashCompleter`. No wire-format or protocol changes.

### Test

- **cli**: 42 new tests in
  `tests/phoson_cli/test_e3_file_mentions_unit.py` — the bounded walk
  (files + dirs, ignored trees, depth/entry caps), mention parsing and
  resolution (text inlining, media blocks, missing/bare/email tokens,
  dedupe, tilde/absolute, size + count caps, string cwd), the
  `PathCompleter` (offer/filter/start-position/mid-sentence/size
  hint/lazy walk/negative cases), and the controller wiring (inline
  file, attach image, notify on attach, warn on missing, silent on
  bare handle, plain text unchanged, `/attach` + mention combined).
  Suite now 1138 passing, pyright 0 errors, ruff clean.

## v0.12.2 (2026-08-25)

### Feat

- **cli**: live subagent metrics in the running panel (IMPROVEMENTS.md
  E2). The parallel-subagent panel now shows Time / Tokens / Cost per
  task in real time instead of static "waiting" cells that only fill in
  at the final summary.
  - *Producer.* The `agent`/`agents` tools now own a
    `SubagentProgressTracker` per call and feed it live from the inner
    runs' `AgentStepDoneEvent` steps (new `on_event` callback on the
    sub-agent stream driver). `finalize` snaps the exact same numbers
    the summary wire format reports (sum of step durations,
    `AgentRunResult` tokens/cost), so the live panel and the final
    summary stay consistent. Timeouts/errors/cancellation mark the row
    failed (✗) without breaking the run (PR #90).
  - *Transport.* The tracker is pushed to the UI through a new
    `on_subagent_progress` sink callback injected via
    `AgentContext.extra`; the sub-agent tools stay UI-agnostic (no
    callback in one-shot/scripts/tests → unchanged behavior). The
    notification happens mid-run, after `AgentStartEvent` created the
    in-flight turn, so there is no race with the pre-provider
    placeholder.
  - *Consumers.* Full-screen sink: `CurrentTurn.subagent_progress` +
    `on_subagent_progress` — running rows tick on the wall clock
    between LLM steps and freeze at their reported final duration, while
    queued rows (waiting on the parallelism semaphore) keep "waiting".
    Classic REPL: `SubagentSpinner.set_progress()` — the animation
    thread re-reads the tracker every frame.
  - *Wire format untouched.* `format_metrics_line` /
    `parse_subagent_metrics` / `render_subagent_summary` are unchanged;
    the final `--- METRICS: ...` summary remains the transcript source
    of truth.

### Test

- **cli**: 23 new tests in
  `tests/phoson_cli/test_subagent_live_metrics.py` — tracker state
  machine (register/start/update/finalize/mark_error, LLM-steps-only
  accumulation, final snap, elapsed bounds), the tools feeding the
  tracker *live* (intermediate values visible mid-run, per-call tracker
  isolation across repeated calls, timeout/error, callback
  notified/cleared), running-table rendering (live values, queued
  "waiting", done ✓ / error ✗, fallback without tracker, tracker vs
  plain list), controller wiring (callback injected into
  `context.extra`), and the full-screen sink (panel renders from the
  tracker, falls back to "waiting" when cleared, tracker survives a
  pre-tool-start notification). Suite now 1096 passing, pyright 0
  errors, ruff clean.

## v0.12.1 (2026-08-25)

### Feat

- **agent/cli**: advanced context management (IMPROVEMENTS.md E1) —
  retained reasoning, structured compaction, large tool-output offload,
  and fine-grained compaction control.
  - *Retained reasoning.* When a compaction (automatic or `/compact`)
    summarizes a segment, the model's captured reasoning from that segment
    (previously persisted to `node.metadata["reasoning"]`) is now folded
    into the summary prompt, so the handoff document keeps the *why* behind
    key decisions, not just the conclusions. The controller registers the
    active run's reasoning before the run and clears it when the run ends,
    regardless of terminal state. This is the technique OpenAI reports
    lifting long-task continuity (13.3%→38.3% on ARC-AGI-3).
  - *Structured compaction.* The summary is now a fixed-section handoff
    document (Goal / Completed / Key decisions / Reasoning highlights /
    Open questions / Next steps / Constraints and context) instead of a
    free-form paragraph (Anthropic long-running-agents pattern), so the
    next context segment consumes it reliably. Auto and manual paths share
    one prompt builder, so they always produce the same artifact.
  - *Large tool-output offload.* A new `OffloadMiddleware`
    (`phoson_agent.plugins.offload`) writes any tool result over a
    configurable size (default 24 KB) to `~/.phoson/compacted/` and leaves
    only a head/tail preview plus the file path in context; the model can
    `read_file` the path to retrieve the full content. Implemented as a
    middleware (not tool logic) per repo principle #2, best-effort (a write
    failure never breaks the run), and toggleable.
  - *Fine-grained control.* New `compact_mode` setting
    (`balanced` | `aggressive` | `off`) with presets (aggressive compacts at
    65% of the window and keeps a shorter tail), plus explicit
    `compact_threshold`, `compact_min_keep_messages`, `offload_*` knobs —
    all env/file-configurable. `/compact on|off` toggles automatic
    compaction at runtime and persists to `config.toml`.
  - *`/compact` preview + confirm.* `/compact` now shows what it *would*
    do ("summarize N of M turns, keeping K, ~T tokens") and asks before
    paying for the summary call; `/compact aggressive` does the same with a
    deeper cut; `/compact yes` applies the last preview without asking.

### Test

- **agent**: unit tests for the offload middleware
  (`tests/phoson_agent/test_offload.py`) and for E1 summarizer behavior —
  structured prompt, retained-reasoning formatting/alignment, `auto_enabled`
  pass-through, and `build_compaction` layout
  (`tests/phoson_agent/test_summarizer_e1.py`).
- **cli**: config-preset tests (mode presets, explicit-value-wins, invalid
  mode fallback, persistence), controller tests for `plan_compaction` /
  profiles / `set_compact_mode` / retained-reasoning registration, and
  `/compact` command tests (preview→confirm, cancel, profile pass-through,
  noop, mode switch, run-in-flight) in
  `tests/phoson_cli/test_e1_context_unit.py`.

## v0.12.0 (2026-08-25)

### Feat

- **cli/llm**: `xhigh` and `max` reasoning-effort levels, end to end.
  A single source of truth (`phoson_llm.schemas.REASONING_EFFORTS` +
  `ReasoningEffort` type alias) now feeds the `/reasoning-effort`
  command, the session-controller guard and the full-screen
  autocomplete, so they can no longer drift apart. Previously the new
  values were accepted by the command but silently dropped by the
  controller guard before the request; `ModelConfig.reasoning_effort`
  is widened accordingly and OpenAI-compatible backends forward the
  value as-is (PR #86).
- **agent**: vLLM context-window resolution. `ContextWindowResolver`
  now queries the vLLM `/v1/models` endpoint and reads `max_model_len`
  (matching the model `id`) instead of falling back to the 128k
  default — a vLLM server serving a 256k model (e.g. Qwen3.8-27B) is
  now sized correctly for summarization and context display. Same
  soft-fail policy as Ollama/OpenRouter: per-model cache, warning on
  unreachable server or missing model, default fallback (PR #86).

## v0.11.0 (2026-08-25)

### Feat

- **cli**: cross-platform clipboard and text fallback on Ctrl+V
  (IMPROVEMENTS.md D3) — added macOS clipboard support (`pngpaste` for
  images via `brew install pngpaste`, `pbpaste` for text); when the
  system clipboard does not contain an image, Ctrl+V now reads plain
  text from the OS clipboard and inserts it at the cursor position
  instead of swallowing the event; missing `pngpaste` on macOS shows an
  actionable install hint.
- **cli**: `--version` flag prints the installed version (from
  `importlib.metadata`) and exits (IMPROVEMENTS.md D5).
- **cli**: `--classic` / `--no-fullscreen` flags force the classic
  line-by-line REPL for a single run instead of the default full-screen
  TUI (IMPROVEMENTS.md D2); when the interactive terminal reports
  `TERM=dumb` (or no `TERM` at all), the classic REPL is now selected
  automatically with a notice on stderr, since the full-screen
  `Application` needs real cursor/alternate-screen capabilities.
- **cli**: `--model`, `--provider`, `--theme` and `--max-turns` flags
  override the config for a single run without touching
  `~/.phoson/config.toml` (IMPROVEMENTS.md D5); argument parsing is
  centralized in the pure, unit-testable
  `phoson_cli.__main__.parse_args(argv) -> CliOptions` (manual parsing
  kept, no new dependency).

### Test

- **cli**: end-to-end and visual regression test suite for the full-screen
  TUI (IMPROVEMENTS.md D4) — added real key-event routing tests via
  prompt_toolkit's `create_pipe_input` (`Ctrl+J` multiline insert,
  `Ctrl+L` clear, `Ctrl+C` interrupt vs exit, `Escape` cancel); headless
  full agent turn lifecycle test against mock streaming events; and golden
  ANSI rendering snapshots (empty transcript, active streaming turn, error
  panel with hint, completed tool card).


- **cli**: architectural debt cleanup (IMPROVEMENTS.md D1) — removed the
  leftover empty `phoson_cli/textual/` directory; unified the duplicated
  provider-label tables into `phoson_cli/labels.py` (the picker's table,
  with its `grok`/`google`/`aws` aliases, is now the single source and the
  setup wizard reads from it); unified the braille spinner frames into
  `phoson_cli/animations.py` (renderer, subagent panel and the full-screen
  sink now share one sequence); moved the token-usage indicator into
  `formatting.format_token_indicator(used, window)` shared by both front
  ends; moved the slash-command completer into `commands.SlashCompleter`
  (sourced from `COMMAND_SPECS`, imported by both front ends, re-exported
  from `fullscreen.completer` for compatibility); removed the deprecated
  no-op `branch_session` from both the REPL and the controller (breaking
  minor — see migration note); `TODO.md` header/status sections updated
  and its stale `repl.py` LOC figure corrected.

## v0.10.0 (2026-08-25)

### Fix

- **cli**: completed full-screen tool cards now replace their live start line
  in place (keyed by `tool_call_id`) instead of appending a second identical
  headline; parallel calls replace only their own card. The header is now
  deliberately lightweight (brand · transient attachment/memory indicators ·
  live run status), while model (provider), cwd, token usage and session
  cost are consolidated into that header; the lower footer now contains
  keyboard shortcuts only.

### Feat

- **cli**: rich tool cards (IMPROVEMENTS.md C1) — tool calls now render as
  actionable cards instead of raw name+preview lines: human verbs
  ("writing file", "running command", "editing file"), a detail line with
  path/command/query, colored unified diffs for `patch_file`, created/updated
  summaries (`lines · size`) for `write_file`, and first stdout lines for
  `bash`. Cards are pure Rich renderables in `formatting.py`, shared by the
  classic REPL and the full-screen front end; front ends remember start-event
  args keyed by `tool_call_id` so done cards can render their detail.
- **cli**: `/compact`, `/status` and `/resume <id>` commands (IMPROVEMENTS.md
  C2). `/compact` forces an LLM summary now and rewrites the conversation as a
  new branch off the root (old branch preserved, visible in `/tree`),
  reporting tokens before → after and % saved; short sessions are refused
  gracefully. `/status` consolidates provider · model · session · cost ·
  tokens · context window · steps · MCP servers · permissions into one view
  (`/env` `/cost` `/tokens` `/steps` keep working). `/resume <id-prefix>`
  loads a session directly with prefix matching, ambiguity listing, and
  inline autocomplete of saved session ids in the full-screen app.
- **cli**: `web_fetch` tool (IMPROVEMENTS.md C3) — fetches a URL and returns
  readable text (HTML stripped to plain text via stdlib only, ~50 KB cap,
  binary content types rejected); `web_search` gains configurable backends:
  DuckDuckGo (default, keyless), Brave (`BRAVE_API_KEY`) and Tavily
  (`TAVILY_API_KEY`), selected via `PHOSON_WEB_SEARCH_BACKEND` or auto-detected
  from whichever key is present. Both tools integrate with the A1 permission
  model.
- **cli**: consolidated runtime header + look & feel pass
  (IMPROVEMENTS.md C4) — the top bar shows `model (provider)` · cwd ·
  `$session cost` · tokens used/window alongside transient indicators and
  live status; the lower footer contains keyboard shortcuts only. `/help`
  renders grouped by category (Session / Model / Info / Config & System);
  `/tree` prints a colored tree (current node accented, abandoned branches
  muted, labels highlighted) as a shared Rich renderable; error panels gain
  an actionable hint line for known codes (`auth` → run /setup,
  `rate_limit` → wait or switch model, `max_iterations` → raise budget).

## v0.9.1 (2026-08-25)

### Fix

- **cli**: the full-screen multiline composer now wraps long pasted or typed
  lines instead of scrolling them out of view, and an empty composer occupies
  one line rather than expanding to its five-line cap
- **cli**: the chat shows a transient animated activity line immediately after
  Enter (before the provider's first event), rotates short thinking phrases
  while waiting, reports streaming/tool/subagent phases, and clears on
  completion or cancellation; hidden reasoning no longer duplicates a
  `Phoson / thinking...` placeholder

## v0.9.0 (2026-08-24)

### Feat

- **agent**: per-tool permission model (IMPROVEMENTS.md A1, phase 1) — a
  new `PermissionMiddleware` gates every tool call through the standard
  `on_before_tool` hook with three levels (`allow` / `ask` / `deny`) plus
  per-tool glob allow-patterns (e.g. `bash: ["git status", "pytest*"]`);
  `ask` routes to the front end's confirmation and fails closed without
  one; denials surface as an actionable tool result (`permission_denied`)
  telling the model how to proceed instead of a generic block
- **cli**: durable policy in `~/.phoson/permissions.json` and a new
  `/permissions` command to list or change tool levels on the fly
  (`/permissions bash ask`); changes persist immediately; one-shot mode
  fails closed for `ask`-level tools
- **cli**: AGENTS.md filesystem memory (IMPROVEMENTS.md A3) — an
  `AGENTS.md` at the repo root (or any directory between root and cwd),
  a global `~/.phoson/AGENTS.md`, or a `CLAUDE.md` alias is injected into
  the system prompt every turn; supports `@file` imports, caps content at
  ~2000 tokens with a visible truncation marker, and re-reads files each
  turn so edits apply immediately; new `/agents-md` command lists what is
  loaded and the TUI header shows a 📄 indicator when memory is active
- **cli**: pressing Enter while a turn is already running now shows a warn
  notice ("A turn is already running — press Esc to cancel it first") and
  keeps the typed text in the input, instead of silently ignoring it
  (IMPROVEMENTS.md A4)
- **cli**: multiline input in the full-screen TUI — `Ctrl+J` inserts a
  newline (`Enter` still sends) and the input grows up to 5 lines before
  scrolling internally; input history now persists to
  `~/.phoson/history.txt` (the same file the classic REPL uses), so `↑`
  recalls messages from previous sessions after a restart
  (IMPROVEMENTS.md A2)

## v0.8.1 (2026-08-24)

### Fix

- **cli**: system prompt now uses the *system* timezone instead of a
  hardcoded `America/Mexico_City` — the clock is derived from the process's
  local zone (honours `TZ`), with a UTC fallback, so users outside CDMX no
  longer get a wrong time in the agent's context (IMPROVEMENTS.md B1)
- **cli**: system prompt tool list is now derived from the actual tool
  registry (`sorted` names) instead of a hardcoded string, so it can never
  advertise removed tools or omit new ones (IMPROVEMENTS.md B2)
- **cli**: destructive session deletes now ask for confirmation — the
  session picker's `d` / `X` (delete marked) and `/delete <id>` all route
  through the existing confirm prompt and delete nothing when declined; the
  picker footer reads `X delete marked (asks)` (IMPROVEMENTS.md B3)

## v0.8.0 (2026-08-24)

### Feat

- **cli**: `Esc` cancels the in-flight run in the full-screen app (#68) —
  same semantics as the existing cancel path (partial progress saved,
  session stays open); no-op when idle so Float/autocomplete dismissal
  is unaffected
- **cli**: sub-agent model fallback (#61) — when `subagent_model` fails
  with an availability error (404 / deprecated / no endpoints), the task
  automatically retries once on the main agent's model; auth (401/403)
  and rate-limit (429) errors deliberately do not fall back
- **cli**: fallback visibility — the parallel-agents summary marks
  fallback agents with `✓ ↻` in warning style and a `⚠ fallback: ...`
  caption; sequential `agent` results carry a `[fallback to <model>]`
  note; real metrics are reported for fallback runs
- **cli**: suppress logging's stderr last-resort handler while the
  full-screen TUI runs, so raw provider errors can no longer corrupt
  the UI
- **cli**: system prompt now includes the OS platform, current time and
  timezone

## v0.7.3 (2026-08-24)

### Feat

- **cli**: lightweight `/provider` and `/sessions` pickers (#55) —
  `/provider` gains inline fuzzy autocomplete of enabled providers;
  `/sessions` prints a compact numbered list and `/sessions load <n>`
  loads a session by number with an autocomplete dropdown; the Float
  picker remains via `/sessions pick`
- **cli**: session picker multi-delete — space marks sessions, X deletes
  all marked without closing the window
- **cli**: session titles — auto-generated from the first user message,
  overridable with the new `/title` command, shown in every session list

## v0.7.2 (2026-08-24)

### Perf

- **cli**: cache immutable transcript blocks as ANSI strings, re-rendering
  only new blocks per frame — ~18x faster on long transcripts
- **cli**: stream the in-flight answer as plain text and apply the full
  Markdown render once when the turn is frozen, instead of re-parsing
  growing content every frame
- **cli**: coalesce streaming repaints to ~16fps with a guaranteed
  trailing repaint; structural events still repaint immediately
- **cli**: reuse a single Rich `Console` instance across cached block renders

### Fix

- **llm**: Gemini adapter — inline local `file://` image/PDF attachments as
  base64 instead of passing a local path as a hosted URI, and emit visible
  text placeholders for unsupported block types instead of dropping them (#53)
- **cli**: replay the full conversation path when resuming a session instead
  of a fixed 6-message tail; very long histories cap at 200 messages with an
  explicit truncation notice (#56)

## v0.7.1 (2026-08-24)

### Fix

- **cli**: persist the Ctrl+T reasoning-visibility default across sessions
  (`show_reasoning` config field, `PHOSON_SHOW_REASONING` env var) (#50)
- **cli**: `/attach` now rejects files over 20MB with a clear error instead of
  reading and encoding them silently, and warns when a file type (PDF, video)
  degrades or is dropped by the active provider (#54)
- **config**: add regression tests proving `/model` and `/reasoning-effort`
  persist across CLI restarts (#49)

## v0.7.0 (2026-08-23)

### Feat

- **cli**: paste images from the clipboard with Ctrl+V
- **agent**: add a view_image tool so the agent can see images
- **cli**: replace Textual TUI with a native prompt_toolkit full-screen front end

### Fix

- **cli**: fix pre-existing pyright/ruff-format failures from the TUI migration
- **cli**: refresh context window immediately on /model and /provider

## v0.6.0 (2026-08-20)

### Feat

- **cli**: TUI phase 4 — the Textual TUI now runs the *same*
  `CommandHandler`/`COMMAND_SPECS` as the classic REPL via a new
  `CommandHost` protocol (`RendererCommandHost` for Rich/prompt_toolkit,
  `TextualCommandHost` for the TUI): `/help /new /tree /undo /label
  /attach /env /cost /tokens /steps /model /provider /sessions /delete
  /mcp /update /exit` work identically on both front ends.
- **cli**: native Textual pickers — `ModelPickerScreen` (fuzzy filter),
  `ProviderPickerScreen` and `SessionPickerScreen` (load / `d` delete),
  replacing the prompt_toolkit pickers inside the TUI.
- **cli**: multiline composer — `Enter` sends, `Shift+Enter` inserts a
  newline, `Tab` completes slash commands; the composer is disabled
  while a turn runs and re-focused when it ends.
- **cli**: tool cards are keyed by `tool_call_id` so parallel tool calls
  no longer clobber each other's results; `SubagentStatusPanel` shows
  parallel sub-agent tasks live.
- **cli**: session resume in the TUI replays the *tail* of the history
  (was: the head, and the replay was wiped after `print_history`).
- **cli**: `/model <id>` and `/provider <id>` persist to
  `~/.phoson/config.toml` (parity with the classic REPL); `/update`
  confirmation is injectable (works inside the TUI).

### Fix

- **cli**: Kitty/Alacritty input — in addition to the associated-text
  workaround, "report all keys" is now disabled too: without associated
  text it delivers Shift+digit with no character, so the Spanish `/`
  (Shift+7) could not be typed. GNOME Terminal was unaffected.
- **cli**: user/tool text is markup-escaped in TUI rows (a message
  containing `[red]` can no longer restyle the conversation).
- **cli**: the safe-mode bash modal now shows the command itself
  instead of duplicating the prompt text.
- **cli**: `Ctrl+L` during a run no longer destroys the live
  `StreamingTurn` (asks to cancel first).

## v0.5.0 (2026-08-19)

### Feat

- **cli**: one-shot mode, /undo command, sub-agent concurrency and timeout
- **cli**: /update command and shared self-update flow

### Fix

- **cli**: remove dead /branch command, close old engine on rebuild, harden config

## v0.4.0 (2026-08-19)

### Fix

- **examples**: update MCP plugin path after _plugin.py rename
- resolve provider SDK breakages, type-safety, logging, and refactors

## v0.3.0 (2026-08-09)

### Feat

- **memory**: close remaining phoson_plugin_memory gaps (prefix, CRUD tools, auto-purge)
- **memory**: add Qdrant semantic tier to phoson_plugin_memory
- **memory**: add Postgres long-term tier to phoson_plugin_memory
- **plugins**: add checkpoint/memory plugins, fix MCP session pooling

### Fix

- **cli**: preserve existing config on startup

## v0.2.4 (2026-05-15)

### Fix

- **gemini**: avoid leaking api key in model listing

## v0.2.3 (2026-05-15)

### Feat

- **cli**: stream markdown with rich live

## [v0.2.2] (2025-07-17)

### ✨ Features

#### phoson_cli — Full AI Provider Support

- (`provider_picker.py`) Expand provider picker from 4 to all 19 providers with
  labels for: GitHub Models, NVIDIA, Grok (X.AI), Groq, DeepSeek, Together AI,
  Perplexity, LM Studio, vLLM, Azure OpenAI, Google Gemini, Mistral AI,
  AWS Bedrock, Fireworks AI, Cohere
- (`model_selector.py`) Add model listing functions for all 15 new providers with
  automatic API discovery and graceful fallback on errors
- (`installer.py`) Expand setup wizard (`/setup`) to support all 19 providers:
  - Provider selection now shows all 19 providers to toggle
  - Credential prompts for each provider's API key / base URL
  - Summary table displays all configured providers and credentials
  - `_infer_enabled_providers` detects credentials for all 19 providers

### 📦 Version

- Bump version to 0.2.2

## [0.1.0] (2025-05-02)

### ⚡ Highlights

- First stable release of `phoson-engine-minimal`
- Framework-free Python runtime for the Phoson autonomous-agent platform
- Multi-provider LLM support: OpenAI, Anthropic, OpenRouter, Ollama

### ✨ Features

#### phoson_llm — LLM Normalization Layer
- (`21767dd`) Add utils module and public API exports
- (`2226570`) Add OllamaChat adapter for local LLM inference
- (`572da54`) Implement OpenRouterChat with new tool handling
- (`a238d3e`) Add multimodal input blocks for images, audio, video, documents
- (`9f22a2b`) Add subagent support with label field and AgentSubagentResult model
- (`d2130ec`) Increase max_tokens limit in ModelConfig to 32,768
- (`ac078f1`) Implement attachment manager for multimodal files in CLI
- (`597d7fb`) Add patch_file tool and line range params to read_file

#### phoson_agent — Agent Orchestration
- (`91a4261`) Add summarization and context window middleware plugins
- (`dfee0f8`) Add session metadata tracking and persistence in JSONL
- (`9f22a2b`) Add subagent support with label field and AgentSubagentResult model

#### phoson_cli — Interactive REPL
- (`605646a`) Add session delete command, load session, and token indicator
- (`c2e6c7d`) Add interactive session picker with pagination
- (`8ea0c33`) Add subagent tools (agent, agents) with build_tools_dict helper
- (`cc21ab5`) Add live subagent panel rendering and session metrics commands
- (`d4e4c93`) Add parallel sub-agent execution with metrics and UI

### 🐛 Bug Fixes

- (`740ad4b`) Update get_weather handler to accept context parameter
- (`a794177`) Improve formatting of metrics output in agents function
- (`afc079f`) Fix formatting issues

### 📚 Documentation

- (`7ef04f0`) Translate all docstrings to English
- (`e4ec1ab`) Improve docstring formatting for AgentEngine class
- (`c58b20e`) Add comprehensive docstrings to phoson_agent modules
- (`03897bf`) Add comprehensive docstrings to phoson_llm modules
- (`c39aae1`) Add comprehensive docstrings to all CLI modules
- (`bd23d10`) Expand README with full documentation and add CONTRIBUTING

### 🎨 Style

- (`c228160`) Apply ruff formatting to all modules
- (`4490006`) Fix import sorting with ruff

### 🔧 Refactor

- (`c39aae1`) Unify SessionMetrics and add BaseTool interface
- (`4fe6fe8`) Improve JSON schema generation for complex types
- (`168aac2`) Improve code readability and organization in various modules

### ✅ Tests

- (`1e0e177`) Improve formatting and readability in integration tests
- (`c067900`) Add edge case coverage (tool errors, empty response, max iterations, LLM protocol errors)
- (`3bf244b`) Add provider adapter integration coverage (OpenAI, OpenRouter, Anthropic, Ollama)
- (`7a2bd5f`) Add OpenAI adapter integration test
- (`060c1d0`) Add tests for subagent tools and renderer functionality
- (`c5b4e0f`) Add multimodal input tests for images, audio, video, and documents

### 📦 Dependencies

- Initial dependency set includes: anthropic, httpx, openai, prompt-toolkit, rich, tiktoken
