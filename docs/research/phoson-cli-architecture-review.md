# phoson-cli architecture review (2026-10-22, v0.53.0 @ 49bdd77)

Handoff notes for refactoring `phoson_cli`. Each issue lists the evidence
(verified file:line at the commit above), why it matters, a proposed fix,
and an acceptance check. Line numbers drift — re-grep before editing.

Package size at review time: ~32.8k lines in `phoson_cli`, 107 test files in
`tests/phoson_cli` (+10 in `tests/phoson_cli/fullscreen`).

## Intended design (keep this)

- `SessionController` (`controller.py`) owns the runtime: chat client,
  engine, tools, plugins, session tree, persistence, run lifecycle.
- Presentation goes only through `AgentEventSink` (`ui_protocols.py`);
  classic REPL uses `ClassicSink` (`renderer.py`), full-screen uses
  `FullScreenSink` (`fullscreen/sink.py`).
- Interactive confirmations go through `ConfirmationService`; when none is
  injected (one-shot) tools fail closed.
- `CommandHandler` (`commands.py`) owns command semantics; a `CommandHost`
  (`command_host.py` classic, `fullscreen/command_host.py` TUI) owns how
  effects are shown.
- Startup laziness: `phoson_cli/__init__.py` and `__main__.py` use PEP 562
  `__getattr__` so `import phoson_cli.config` / `--help` don't load
  prompt_toolkit/rich. Guarded by `tests/phoson_cli/test_lazy_cli_imports.py`
  (runs probes in a clean subprocess — reuse this pattern for new guards).

Strengths worth preserving: narrow sink protocol, fail-closed confirmations,
pure `parse_args` → `CliOptions`, durable mid-turn checkpoints
(`_synced_history_count`), lazy imports with regression tests.

## Status (2026-10-22)

| Issue | State | Where |
|---|---|---|
| 3 Shared runtime builder | Done | `phoson_cli/runtime.py`; one-shot now also gets the reasoning-sandwich scheduler and summarizer tool schemas (previously missing) |
| 1 UI leak into controller | Done | `command_catalog.py`, `tool_render.py`, `tools/subagent_metrics.py`; lazy imports in `theme.py`, `plugin_ui.py`. Guard: `test_lazy_cli_imports.py::test_session_runtime_*` (blocks `httpx._main`, which imports rich on its own) |
| 2 TUI layered on REPL | Done | `session_frontend.py` (`SessionFrontend`), `ui_protocols.SessionPort`; `PhosonApp.session` replaces `PhosonApp.repl`; `CommandHandler.session` replaces `.repl`; REPL private passthroughs removed |
| 5 Hidden config attrs | Done (theme attrs) | `_startup_theme*`, `_cli_theme_deferred` are declared fields. Provider `getattr`s in `save_config` kept on purpose (duck-typed configs) |
| 6 Plugin builders | Done | `official_plugins.py` table + `load_official_plugin`; `build_*_plugins` kept as one-line wrappers |
| 7 Small items | Partly | Shims deleted; plugin wiring moved out of `session_utils`. Tools-package location left as a decision |
| 4 God modules | Partly | `SessionTitler` extracted (`session_titles.py`); command catalog extracted. **Open:** split `CommandHandler` by command group, nested config dataclasses, wake loop / compaction / navigation out of the controller |

## Recommended order

1. Issue 3 (shared runtime builder) — lowest risk, already caused a bug.
2. Issues 1 + 2 (UI leak, `SessionPort`) — restore real layering.
3. Issues 5 + 6 — mechanical cleanups.
4. Issue 4 — incremental split of god modules, controller first.

Do one issue per PR. Run `uv run pytest tests/phoson_cli`, `uv run ruff
check`, `uv run ruff format --check`, `uv run pyright` before each push (CI
enforces ruff format and pyright — see commit f55cae0).

---

## Issue 1 — "UI-independent" controller imports prompt_toolkit and rich

**Evidence**
- `controller.py:1-14` docstring claims it is free of presentation deps.
- `phoson_cli/__init__.py` docstring promises non-interactive hosts (embedded
  Phoson-Core) can import config/factory helpers without the TUI stack.
- But `controller.py` imports:
  - `.commands` → `commands.py:30-31` imports `prompt_toolkit.document` /
    `prompt_toolkit.completion` (for `SlashCompleter`, `PathCompleter`,
    `commands.py:538,574`). Controller only needs `build_command_catalog`.
  - `.formatting` → `formatting.py:21-26` imports rich. Controller only needs
    `ToolRenderRegistry` / `build_tool_render_registry`.
  - `.plugin_ui` → `plugin_ui.py:7-9` imports rich (`SinkPluginUiService`).
  - `.theme` → `theme.py:39,41` imports rich `Style` and prompt_toolkit
    `Style` (`ThemeRegistry`, `build_theme_registry`).
- Verified:
  ```
  .venv/bin/python -c "import sys, phoson_cli.controller; \
    print(any(m.startswith('prompt_toolkit') for m in sys.modules), \
          any(m.startswith('rich') for m in sys.modules))"
  # → True True
  ```

**Fix**
- Split `commands.py`: move `CommandSpec`, `COMMAND_SPECS`, `CommandCatalog`,
  `RegisteredPluginCommand`, `build_command_catalog`, `parse_command`,
  `is_run_safe_command` into a UI-free module (e.g. `command_catalog.py`);
  keep completers + `CommandHandler` where they are (re-export for compat).
- Make registry data types UI-free: move `ToolRenderRegistry` and the
  `ThemeRegistry` data structure into modules that don't import rich/pt at
  module level (convert to rich/pt styles lazily at the presentation edge),
  or have the front end inject the registry builders into the controller.
- `SinkPluginUiService`: import lazily or move the rich rendering into the
  sink.

**Acceptance**
- New probe in `test_lazy_cli_imports.py`: `import phoson_cli.controller`
  loads none of `HEAVY_UI` (`prompt_toolkit`, `rich`, `pygments`).

## Issue 2 — Full-screen front end is layered on top of the classic REPL

**Evidence**
- `fullscreen/app.py:54` `from ..repl import PhosonRepl`; `app.py:374`
  `self.repl = PhosonRepl(...)`; `app.py:382`
  `CommandHandler(self.repl, host=FullScreenCommandHost(self))`.
- `repl.py:285-330` — block of passthroughs to controller *private* state:
  `_cw_resolver`, `_context_window`, `_context_tokens` (with setters),
  `_rebuild_engine`, `_build_user_message`, `_append_user_turn`,
  `_finalize_run`, `_append_partial_history`. Comment says "tests, prompt
  display".
- `commands.py:739` `CommandHandler.__init__(self, repl: "PhosonRepl", ...)`;
  `commands.py:755` reaches `getattr(self.repl, "_controller", None)`.
- `CommandHandler` uses ~25 distinct `self.repl.*` members (most frequent:
  `config` ×56, `tree` ×14, `storage` ×9, `current_model` ×9,
  `session_metrics`, `subagent_model`, `set_model`, `engine`, `attachments`,
  `theme`, `load_session`, plus `_context_window`, `_context_tokens`).
  Get the full list: `grep -o "self\.repl\.[a-z_]*" phoson_cli/commands.py | sort | uniq -c`.
- `fullscreen/command_host.py:288` calls `controller._remember_bash_pattern`.
- `repl.py:64` re-exports `session_utils` helpers (`# noqa: F401`) and
  `__main__._run_oneshot` imports them *from `phoson_cli.repl`*.

**Why it matters**: `PhosonRepl` has become the de-facto session API. Any
TUI change risks the classic REPL and vice versa; private controller state
leaks to three modules.

**Fix**
- Define `SessionPort` Protocol (in `ui_protocols.py` or new
  `session_port.py`) with exactly what `CommandHandler` needs (from the grep
  above). Public names only — promote `context_window`/`context_tokens`
  properties (already exist on controller: `controller.py:425,442`) and add a
  public `remember_bash_pattern`.
- `SessionController` implements it; `CommandHandler(session: SessionPort,
  host: CommandHost)`. Theme belongs to the front end, so put `theme` on the
  host or pass it separately.
- `PhosonApp` builds its own `SessionController` (with `FullScreenSink`)
  instead of a `PhosonRepl`. Check `app.py:583` (`apply_theme` extends the
  REPL's) and `app.py:250`/`:1154` (shared input history, run entry point)
  for behavior to lift into a shared helper.
- Delete the private passthroughs in `repl.py`; update tests that used them
  to go through the controller.
- Keep `CommandHandler(dummy_repl)` test call sites working by making the
  dummy satisfy `SessionPort` (docstring in `command_host.py:1-11` mentions
  this pattern).

**Acceptance**
- `grep -rn "from ..repl\|from .repl" phoson_cli/fullscreen` → nothing.
- `grep -rn "_controller\._\|controller\._" phoson_cli --include=*.py`
  outside `controller.py` → nothing.
- pyright passes with `SessionPort` typed (no `getattr` fallbacks).

## Issue 3 — Engine assembly duplicated between one-shot and controller

**Evidence**
- `__main__.py:644-852` `_run_oneshot` (~210 lines) builds: `build_chat`,
  `build_tools`, `build_offload`, `build_summarizer`,
  `build_permission_middleware` + `build_permission_classifier`,
  `build_middlewares`, `AgentEngine(..., plugins=build_plugin_specs(config))`,
  `apply_tool_hints`, `build_theme_registry`,
  `engine.context.extra["middlewares"]`, `context.extra["available_tools"] =
  build_tools_dict()`, `ModelConfig(system=build_system_prompt(...))`,
  `close_plugins(...)`.
- `controller.py:590-814` `_rebuild_engine` does the same sequence plus
  command catalog / tool render registry / theme registry rebuild and plugin
  teardown of the previous instance.
- `_run_oneshot` docstring cites #174/F-02: one-shot was previously missing
  the permission/summarizer/offload chain — exactly the drift this
  duplication causes.
- `_run_oneshot` imports `close_plugins, build_plugin_specs,
  build_system_prompt` from `phoson_cli.repl` (loads the REPL module and the
  UI stack in one-shot mode). They live in `session_utils.py`.

**Fix**
- Extract `build_runtime(config, *, on_ask, on_decision, plugin_ui,
  extra_middlewares=()) -> Runtime` (dataclass: chat, tools, tools_dict,
  middlewares, permission, engine, loaded plugins) into e.g.
  `phoson_cli/runtime.py`. Order of the middleware chain must stay
  Offload → Summarizer → Permission (+ Trace appended in one-shot).
- `_rebuild_engine` calls it and keeps only controller-specific work
  (catalog/registry refresh, closing old plugins, session-bound context).
- `_run_oneshot` calls it with no `on_ask` (fail-closed) and appends
  `TraceMiddleware`.
- Change one-shot imports to `session_utils`.

**Acceptance**
- Existing tests pass: `test_oneshot_unit.py`, `test_174_permissions_inheritance.py`,
  `test_controller_unit.py`, `test_140_otel_wiring_unit.py`.
- Add a test that one-shot and controller produce the same middleware type
  sequence (minus Trace).
- Lazy-import probe: one-shot path no longer imports `phoson_cli.repl`.

## Issue 4 — God modules

| File | Lines | Concerns mixed |
|---|---|---|
| `controller.py` | 2607, 80 methods | turns/stream, titles (`:1228-1406`), wake loop (`:1624-1757`), compaction (`:1826-2141`), session load/repair (`:2142-2307`), model/provider switching (`:2308-2428`), rewind/jump (`:2429-2600`) |
| `commands.py` | 2371 | specs, completers, ~70 handlers in one `CommandHandler` class (`:733-2287`), `bg list` (`:2288+`) |
| `config.py` | 2147 | `PhosonConfig` 99 flat fields (`:84-344`), `load_config` (`:917-1358`, ~440 lines), `save_config` (`:1465-1880`, ~415 lines), provider credential detection, `build_chat` |

Recent churn (since 2026-07) concentrates in `commands.py`, `model_selector.py`,
`installer.py`, `config.py`.

**Fix (incremental, one extraction per PR)**
- Controller: extract `TitleService`, `WakeLoop`, `CompactionService`,
  `TreeNavigator` (undo/jump/label) as collaborators owned by the
  controller; keep public controller methods as thin delegates at first.
- Commands: `COMMAND_SPECS` already maps names → method names; move handler
  groups into modules (`commands/session.py`, `commands/model.py`,
  `commands/mcp.py` — `_mcp_commands.py` already exists as precedent) and
  register functions instead of methods.
- Config: group fields into nested dataclasses (context/compaction,
  permissions, plugins/feature flags, providers/credentials, UI). Drive
  `load_config`/`save_config` from a field table (key, env var, type,
  default) instead of per-field code. Preserve `save_config`'s
  byte-for-byte preservation of non-managed lines and atomic write.

## Issue 5 — Hidden attributes on `PhosonConfig`

**Evidence**
- `theme.py:630-646`: `setattr(config, "_cli_theme_deferred" | "_startup_theme_registry" | "_startup_theme", ...)`.
- `controller.py:348`: `getattr(config, "_startup_theme_registry", None)`.
- `__main__.py` `_run_oneshot`: `hasattr(config, "_startup_theme_registry")`.
- `config.py:1559,1563`: `getattr(config, "_provider_source", ...)`,
  `getattr(config, "_persisted_provider", ...)`.

**Fix**: declare them as `field(default=None, repr=False, compare=False)` on
the dataclass, or move startup-only state (theme registry/theme/deferred
flag) into a `StartupContext` passed explicitly to front ends. Then remove
all `getattr/setattr/hasattr` on config for these names.

## Issue 6 — Copy-paste plugin builders

**Evidence**: `session_utils.py:474-910` — `build_plugin_specs`,
`build_peers_plugins`, `build_mcp_plugins`, `build_monitor_plugins`,
`build_bgjobs_plugins`, `build_ssh_plugins`, `build_computeruse_plugins`,
`build_swarm_plugins`, `build_questions_plugins`, `build_otel_plugins`. Each:
check `config.enable_X` → import package → instantiate → `configure(dict)` →
`except ImportError: _in_tree_fallback_spec(...)` → `except Exception:
warnings.warn(...)`. Some have extras (SSH checks `SSH_AVAILABLE`; OTEL
checks `_user_specified_otel`).

**Fix**: a table of `OfficialPlugin(flag, module, cls, config_factory,
availability_check=None, disabled_msg)` and one generic loader. Keep
special cases as hooks. Later option: discover via entry points.

**Acceptance**: `test_plugin_decoupling.py`, `test_plugin_manager_unit.py`,
`test_monitor_integration.py`, `test_peers_integration.py` pass unchanged.

## Issue 7 — Smaller items

- Compat shims `fullscreen/session_cache.py`, `fullscreen/model_cache.py`
  only re-export; delete if nothing external imports them
  (`grep -rn "fullscreen.session_cache\|fullscreen.model_cache\|fullscreen import .*cache"`).
- `session_utils.py` (1187 lines) is a grab-bag: git env block, tool usage
  block, system prompt, plugin builders, wake draining, middleware builders.
  Split into `system_prompt.py`, `plugins_official.py`, `runtime.py` (ties
  into Issue 3/6).
- Built-in tools (`phoson_cli/tools/`, incl. `subagent.py` 950 lines,
  `search.py` 770) live in the CLI package, so `phoson_agent` embedders
  can't reuse them. Decide whether they belong in `phoson_agent` or a
  `phoson_tools` package. Check `tools/subagent_panel.py` (UI) stays in CLI.

## Out of scope of this review

- No line-by-line bug hunt was done.
- Uncommitted changes at review time (`updater.py`, `phoson_plugin_stt/engine.py`,
  `scripts/phoson-installer.sh`, related tests) were not reviewed.
