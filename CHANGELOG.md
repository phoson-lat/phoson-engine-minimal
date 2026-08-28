# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

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
