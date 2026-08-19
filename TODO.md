# Technical TODO — phoson-engine-minimal

> **2026-08-19:** The full backlog was closed on branch
> `fix/review-fixes-and-backlog-items`. All 12 original issues
> (#18–#29) are resolved; this file remains as an index of what was
> done and what is left before merge.

## Resolved issues (branch fix/review-fixes-and-backlog-items)

| # | Area | Issue |
|---|------|-------|
| 1 | `ToolHandler` contract documented + regression tests | [#18](https://github.com/phoson-lat/phoson-engine-minimal/issues/18) |
| 2 | `SummarizationMiddleware.wrap_llm_call` tests (real summary, no prompt leakage, no double compaction) | [#19](https://github.com/phoson-lat/phoson-engine-minimal/issues/19) |
| 3 | Docs sweep (README + docs/api + examples) + critical snippets covered by tests | [#20](https://github.com/phoson-lat/phoson-engine-minimal/issues/20) |
| 4 | `ToolCallAccumulator` extracted + 20 direct tests for the OpenAI-compatible helper | [#21](https://github.com/phoson-lat/phoson-engine-minimal/issues/21) |
| 5 | JSON aliases (`JsonValue`/`JsonObject`/`JsonSchema`) applied across schemas + adapters | [#22](https://github.com/phoson-lat/phoson-engine-minimal/issues/22) |
| 6 | Logging policy: module loggers, warning/debug on fallbacks | [#23](https://github.com/phoson-lat/phoson-engine-minimal/issues/23) |
| 7 | Sync API limitations documented (`stream_sync` buffering, `run_sync` loop check) | [#24](https://github.com/phoson-lat/phoson-engine-minimal/issues/24) |
| 8 | `JsonlStorage` async I/O (already used `asyncio.to_thread`; item obsolete) | [#25](https://github.com/phoson-lat/phoson-engine-minimal/issues/25) |
| 9 | `plugin_loader`: `_sys_path_guard` with exact restore + 6 tests | [#26](https://github.com/phoson-lat/phoson-engine-minimal/issues/26) |
| 10 | `plugin.py` → `_plugin.py` in all three plugins (no more shadowing) + documented convention | [#27](https://github.com/phoson-lat/phoson-engine-minimal/issues/27) |
| 11 | Language consistency: public API 100% English; project-wide English policy adopted | [#28](https://github.com/phoson-lat/phoson-engine-minimal/issues/28) |
| 12 | Event audit: `AgentSubagentResult` and `LLMModalitiesEvent` marked experimental | [#29](https://github.com/phoson-lat/phoson-engine-minimal/issues/29) |

## Pending (outside this branch)

- Merge the PR and cut the next release tag (v0.4.0).
- Optional: doctest infrastructure for doc snippets.
- Optional: wire up `AgentSubagentResult` / `LLMModalitiesEvent` when
  their features land (subagent orchestration, modality discovery).

## Decisions

- `ROADMAP.md` stays in Spanish: it is the maintainer's working plan and is
  out of scope for the English-only policy (which covers user-facing
  content: docs, docstrings, comments, commits, issues, PRs, examples).

## Verification (branch)

```bash
uv sync --dev --all-extras
uv run ruff format --check . && uv run ruff check .
uv run pyright
uv run pytest -q
```

Latest verification: `584 passed, 0 skipped` (with test backends up),
`pyright 0 errors`, `ruff clean`.
