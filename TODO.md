# Technical TODO — phoson-engine-minimal

> **2026-08-20:** CLI P1 core (one-shot mode, `/undo`, sub-agent
> concurrency + timeout) merged in PR #34 and shipped in **v0.5.0**.
> Look & feel work is starting: the theme foundation is on branch
> `feat/cli-theme-foundation` (PR pending). This file is the index of
> what is left.

## Resolved (shipped in v0.4.0)

All 12 original backlog issues (#18–#29): `ToolHandler` contract docs +
tests (#18), `SummarizationMiddleware` tests (#19), docs sweep + tested
snippets (#20), `ToolCallAccumulator` extraction (#21), JSON aliases
(#22), logging policy (#23), sync-API limits (#24), `JsonlStorage`
(obsolete, #25), `_sys_path_guard` (#26), `_plugin.py` rename (#27),
English-only language policy (#28), experimental event marking (#29).
Plus: provider SDK bug fixes (Anthropic/Mistral/Gemini), pyright in CI,
user-facing docs translated to English.

## CLI P0 — merged (PR #32, shipped in v0.5.0)

| Area | Fix |
|------|-----|
| Dead `/branch` command | Removed (was a silent no-op: `tree.branch()` returned the same node). `PhosonRepl.branch_session` kept as a deprecated no-op for API compat. Real branching/undo UX is P1. |
| Engine rebuild leak | `_rebuild_engine` now closes the previous engine's plugins (async `aclose()` when available, sync `cleanup()` fallback) and the old chat client — switching model/provider no longer leaks MCP pooled sessions/STDIO subprocesses. Close failures are logged, never fatal. |
| Config file permissions | `save_config` now chmods `config.toml` 0600 and `~/.phoson/` 0700 (it holds API keys). |
| Startup crash on missing credential | `main()` pre-checks `build_chat(config)` and exits 1 with a friendly message + `--setup` hint instead of a traceback. |
| `find_latest_node_id` | Now picks the newest *leaf* (the continuation point) with a deterministic tie-break by node id. |
| Provider list drift | `_has_configured_provider` (main) and `enabled_providers_from_config` (config) now share one credential registry (`_credential_providers` + `NO_CREDENTIAL_PROVIDERS` + new `has_configured_provider`). |
| Stale system prompt | Tool list now says `agent, agents` (real names) and mentions MCP tools when loaded (`_build_system_prompt`). |

## CLI self-update — merged (PR #33, shipped in v0.5.0)

- New `phoson_cli/updater.py`: shared update logic — current version via
  `importlib.metadata` ("dev" from source), latest from PyPI JSON (best
  effort, offline-safe), install-mode detection (uv tool / uvx / pip /
  source / unknown), async subprocess upgrade, per-mode manual hints.
- New `/update` (alias `/upgrade`) slash command: checks, shows
  current → latest, confirms `[y/N]`, upgrades, tells the user to restart.
- `--self-update` flag now uses the same flow (version check first instead
  of a blind `uv tool upgrade`), and exits non-zero on failure.

## CLI P1 core — merged (PR #34, shipped in v0.5.0)

- **One-shot mode**: `phoson-cli "task"` / `-p "task"` / piped stdin —
  single agent run, final content to stdout, exit code 0/1, no REPL and no
  session persistence. Reuses the REPL's shared helpers (system prompt,
  MCP plugins, plugin close). Missing credentials → friendly error (no
  interactive wizard in scripts).
- **`/undo`**: moves the cursor back to just before the last user turn;
  the undone messages stay in the tree as an abandoned branch and the next
  message branches from the restored position. Cost/token metrics are
  cumulative and intentionally not rolled back.
- **Sub-agent concurrency**: `agents` runs behind an
  `asyncio.Semaphore(subagent_max_parallel)` (config key,
  `PHOSON_SUBAGENT_MAX_PARALLEL`, default 4) — the parent agent decides how
  many tasks to spawn, not how many LLM sessions may run at once.
- **Sub-agent timeout**: every sub-agent task (single `agent` and parallel
  `agents`) is guarded by `asyncio.wait_for` with
  `subagent_timeout_seconds` (`PHOSON_SUBAGENT_TIMEOUT`, default 300s);
  a timeout surfaces as a per-task error block, not a hung parent.

## Pending

### CLI look & feel — in progress

Research (2026-08-20) validated staying on Rich + prompt_toolkit with
scrollback (the pattern used by Claude Code / Codex / Gemini CLI);
Textual (full-screen TUI) explicitly rejected. Plan:

- **PR-1 — theme foundation** (branch `feat/cli-theme-foundation`):
  `phoson_cli/theme.py` token system consumed by every rendering site
  (renderer, banner, prompt, pickers, wizard, subagent panel). Four tiers:
  `dark` (default, historical look), `light`, `ansi` (16-color SSH-safe),
  `no-color` (auto via `NO_COLOR`/`CLICOLOR=0`). Selected by
  `PHOSON_THEME` env var or `theme = "..."` in config.toml.
- **PR-2 — tool visibility**: Live execution panel per tool run with
  action labels (`writing file`, `running command`…) + detail line (path,
  size, command), elapsed time, rotating waiting phrases in the spinner,
  and specialized result renderers (colored diff for `edit_file`,
  `+N lines · X KB` for `write_file`).
- **PR-3 — look upgrades**: persistent bottom status bar (model ·
  session · $ · tokens · cwd), colored `/tree`, grouped `/help`, error
  hints, banner animation.

### CLI P1 (planned)

- `/compact` — manual compaction trigger (summarizer currently auto-runs at 80%).
- `/status` — single rich view (provider/model/cwd/cost/tokens/MCP/session) replacing the four atomized commands.
- Configurable system prompt (`PHOSON_SYSTEM_PROMPT` / config.toml).
- `/resume <id>` — direct session load by id (picker only today).

Done so far: `/update` + `--self-update` (PR #33); one-shot mode, `/undo`,
sub-agent concurrency limit + per-task timeout (PR #34).

### CLI P2 (planned)

- `config.build_chat` → delegate to `phoson_llm.factory.build_chat` (remove the duplicated 16-provider if-chain and error-type drift).
- Split `repl.py` (579 LOC god-object) into `RuntimeController` + `SessionManager`; add e2e test for the cancellation path (double KeyboardInterrupt handling is untested).
- Audit/split `installer.py` (625 LOC).

### CLI P3 (no date — design work)

- Per-tool permission model (bash allowlists, ask/never/always levels); runtime `/safe-mode` toggle. Today `safe_mode` is all-or-nothing and only guards `bash`.
- `web_search`: configurable API (Brave/Serper) with DuckDuckGo HTML scraping as fallback (fragile to layout changes).

### Other

- Optional: doctest infrastructure for doc snippets.
- Optional: wire up `AgentSubagentResult` / `LLMModalitiesEvent` when their features land (subagent orchestration, modality discovery).

## Decisions

- `ROADMAP.md` stays in Spanish: maintainer's working plan, out of scope
  for the English-only policy (which covers user-facing content).
- `/branch` removed instead of implemented: with no node navigation the
  command could not do anything meaningful; a real branching/undo UX
  (P1) will replace it.
- `ConversationTree.branch()` kept: it is an honest tree API ("the node to
  use as parent for the new branch"); only the REPL command around it was
  dead.

## Verification

```bash
uv sync --dev --all-extras
uv run ruff format --check . && uv run ruff check .
uv run pyright
uv run pytest -q
```

Latest verification (feat/cli-update-command): `616 passed, 0 skipped` (with
test backends up), `pyright 0 errors`, `ruff clean`.
