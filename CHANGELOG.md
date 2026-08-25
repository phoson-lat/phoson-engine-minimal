# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

## Unreleased

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
