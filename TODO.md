# Technical TODO — phoson-engine-minimal

> **2026-08-19:** Backlog #18–#29 closed and shipped in **v0.4.0**
> (PRs #30 + #31). CLI P0 bug fixes are on branch `fix/cli-p0-bugs`
> (see below). This file is the index of what is left.

## Resolved (shipped in v0.4.0)

All 12 original backlog issues (#18–#29): `ToolHandler` contract docs +
tests (#18), `SummarizationMiddleware` tests (#19), docs sweep + tested
snippets (#20), `ToolCallAccumulator` extraction (#21), JSON aliases
(#22), logging policy (#23), sync-API limits (#24), `JsonlStorage`
(obsolete, #25), `_sys_path_guard` (#26), `_plugin.py` rename (#27),
English-only language policy (#28), experimental event marking (#29).
Plus: provider SDK bug fixes (Anthropic/Mistral/Gemini), pyright in CI,
user-facing docs translated to English.

## CLI P0 — branch `fix/cli-p0-bugs`

| Area | Fix |
|------|-----|
| Dead `/branch` command | Removed (was a silent no-op: `tree.branch()` returned the same node). `PhosonRepl.branch_session` kept as a deprecated no-op for API compat. Real branching/undo UX is P1. |
| Engine rebuild leak | `_rebuild_engine` now closes the previous engine's plugins (async `aclose()` when available, sync `cleanup()` fallback) and the old chat client — switching model/provider no longer leaks MCP pooled sessions/STDIO subprocesses. Close failures are logged, never fatal. |
| Config file permissions | `save_config` now chmods `config.toml` 0600 and `~/.phoson/` 0700 (it holds API keys). |
| Startup crash on missing credential | `main()` pre-checks `build_chat(config)` and exits 1 with a friendly message + `--setup` hint instead of a traceback. |
| `find_latest_node_id` | Now picks the newest *leaf* (the continuation point) with a deterministic tie-break by node id. |
| Provider list drift | `_has_configured_provider` (main) and `enabled_providers_from_config` (config) now share one credential registry (`_credential_providers` + `NO_CREDENTIAL_PROVIDERS` + new `has_configured_provider`). |
| Stale system prompt | Tool list now says `agent, agents` (real names) and mentions MCP tools when loaded (`_build_system_prompt`). |

## Pending

### CLI P1 (planned)

- One-shot / non-interactive mode: `phoson-cli "task"`, `--print`, stdin pipe (scripting/CI).
- `/undo` — step back one turn (the conversation tree already supports it).
- Sub-agent concurrency limit (`asyncio.Semaphore`) + optional per-task timeout.
- `/compact` — manual compaction trigger (summarizer currently auto-runs at 80%).
- `/status` — single rich view (provider/model/cwd/cost/tokens/MCP/session) replacing the four atomized commands.
- Configurable system prompt (`PHOSON_SYSTEM_PROMPT` / config.toml).
- `/resume <id>` — direct session load by id (picker only today).

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

Latest verification (fix/cli-p0-bugs): `594 passed, 0 skipped` (with test
backends up), `pyright 0 errors`, `ruff clean`.
