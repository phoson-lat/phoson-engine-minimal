# phoson_cli

Interactive command-line interface for the Phoson autonomous-agent platform.

## Overview

`phoson_cli` provides an interactive REPL with:

- **Streaming** — Real-time token-by-token output
- **Branching sessions** — Navigate non-linear conversation history
- **Tool execution** — Built-in file, bash, and search tools
- **Subagent support** — Run parallel agent tasks
- **Attachment support** — Images, audio, video, documents

### Architecture (UI-independent session runtime)

The session runtime is decoupled from the front end:

- `SessionController` (`controller.py`) — owns the LLM client, agent
  engine, tools, plugins, session state (tree, cursor, metrics), the
  full run lifecycle (stream, cancellation, partial persistence,
  reasoning capture, saves) and model/provider switching. It has **no
  dependencies on Rich or prompt_toolkit**.
- `AgentEventSink` (`ui_protocols.py`) — the narrow presentation contract
  the controller uses to show anything (events, user turns, notices).
- `ConfirmationService` (`ui_protocols.py`) — interactive yes/no
  contract. The bash tool (safe_mode) receives one through engine
  context injection: the classic REPL injects a prompt_toolkit service
  (`confirmation.py`); front ends that cannot confirm (one-shot) inject
  nothing — the tool then **fails closed** with an actionable message
  instead of hanging or running.
- `formatting.py` / `tools/subagent_panel.py` — pure data→renderable
  formatters shared by every front end.
- `PhosonRepl` (`repl.py`) — the retained classic front end: prompt_toolkit
  input loop, key bindings, completer, prompt display, banner. It adapts
  the Rich `Renderer` to the sink via `ClassicSink` and delegates all
  runtime calls to the controller.
- `PhosonApp` (`fullscreen/app.py`) — the default full-screen
  `prompt_toolkit` front end: persistent scrollable chat pane,
  header/footer, multiline composer, `/model`/`/provider`/`/sessions`
  pickers, and bash confirmation overlay floats. It uses the same
  controller and presentation protocols as the retained classic REPL.

## Running the CLI

```bash
phoson-cli
# or
python -m phoson_cli
```

### One-shot mode (scripts and CI)

Run a single task without the interactive REPL. No session is persisted;
the final answer goes to stdout and the exit code is 0 on success, 1 on
agent error.

```bash
phoson-cli "fix the failing tests"      # positional task
phoson-cli -p "summarize this repo"     # --print flag
echo "explain the CI failure" | phoson-cli   # piped stdin
```

### Command-line flags

One-off overrides for a single run — they never modify
`~/.phoson/config.toml`. Precedence: flag > config.toml > env > default.

| Flag | Effect |
|------|--------|
| `--version` | Print the version and exit 0 |
| `--model <id>` | Override the model for this run |
| `--provider <id>` | Override the provider for this run |
| `--theme <tier>` | Override the theme: `dark`, `light`, `ansi`, `no-color` |
| `--max-turns <n>` | Override `max_iterations` for this run |
| `--classic` | Use the classic line-by-line REPL instead of the full-screen TUI |
| `--no-fullscreen` | Alias of `--classic` |
| `-p`, `--print` | One-shot mode: print the final answer and exit |
| `--setup` | Run the setup wizard |
| `--self-update` | Check for and install CLI updates |
| `--uninstall` | Uninstall phoson-cli |
| `-h`, `--help` | Show usage and exit 0 |

The full-screen TUI is the default interactive front end. `--classic`
launches the retained classic REPL (Rich scrollback, line-by-line
streaming) — useful for debugging and on terminals without full-screen
support. When `TERM` is unset or `dumb` on an interactive terminal, the
classic REPL is selected automatically with a notice on stderr.

## Configuration

### PhosonConfig

```python
from phoson_cli.config import PhosonConfig

config = PhosonConfig(
    provider="openai",           # openai, anthropic, openrouter, ollama
    model="gpt-4o",
    sessions_dir="./sessions",
    max_iterations=12,
    safe_mode=False,
    subagent_max_parallel=4,     # max concurrent sub-agent LLM sessions
    subagent_timeout_seconds=300.0,  # per sub-agent task timeout
)

# Provider API keys are typically loaded from environment variables,
# for example: OPENAI_API_KEY or OPENROUTER_API_KEY.
```

### Theme (appearance)

Four tiers, selected by `PHOSON_THEME` (env var) or `theme = "..."` in
`~/.phoson/config.toml` (env var wins):

| Theme | Use case |
|---|---|
| `dark` (default) | The historical purple palette on dark terminals |
| `light` | Light terminals (Rich never auto-inverts) |
| `ansi` | 16-color-safe palette for SSH / limited-color terminals |
| `no-color` | Plain text; selected automatically by `NO_COLOR` / `CLICOLOR=0` |

`NO_COLOR` (set, non-empty) and `CLICOLOR=0` always win over every other
selection — scripts and CI get plain output. Unknown theme names warn and
fall back to `dark`.

### Key bindings (customizable, full-screen TUI)

The TUI's global key map is built from
`phoson_cli.fullscreen.keys.DEFAULT_KEY_BINDINGS` and can be remapped via
the `[keys]` section of `~/.phoson/config.toml`:

```toml
[keys]
toggle_reasoning = "c-x"          # single sequence
line_up = ["s-up", "c-up"]        # list = precedence order
submit = ""                       # unbind the action
```

Rules:

- One line per **action** (the 15 defaults: `submit`, `newline`,
  `page_up`, `page_down`, `line_up`, `line_down`, `scroll_home`,
  `scroll_end`, `clear`, `toggle_reasoning`, `ctrl_d`, `paste_image`,
  `escape`, `undo_jump`, `exit`).
- Each value is a prompt_toolkit key sequence (`"c-x"`, `"f13"`,
  `"s-up"`, …); a chord is a space-separated string (`"c-x c-e"`).
- `""` unbinds the action; `[]` is rejected (use `""`).
- `undo_jump` (default `Ctrl+Z`) undoes the most recent rewind jump
  (G1 double-Esc). The composer buffer ignores `Ctrl+Z`, so the key is
  free.
- The double-Esc rewind (G1) is *not* an action of its own: it rides on
  the `escape` key. `escape` is bound eager so a lone Esc mid-run always
  cancels immediately (#68), and the app detects a second `escape` within
  a 1.0 s window (a native `"escape escape"` chord could never fire while
  the single eager `escape` binding consumes each press). The window is
  deliberately larger than prompt_toolkit's `ttimeoutlen` (0.5 s) — that
  value delays delivery of a *lone* Esc to disambiguate it from the start
  of an escape sequence, so the delivered gap between two idle Escs clamps
  to ~0.5 s and a 0.5 s window would miss real double-taps. Remapping
  `escape` moves the single-Esc cancel and the double-Esc rewind
  together; `escape = ""` disables both.
- Sequences are validated at startup by
  `phoson_cli.config.load_key_bindings`: an unknown action, an
  unparseable sequence, or a key bound to two actions raises
  `PhosonKeyBindingsError` (a `PhosonConfigError`) and `main()` exits
  with a one-line message — never a silent fallback to the defaults.
- The section is user-managed: `save_config` never writes or removes it.
- `/keys` prints the effective map (defaults or your remaps) and the
  config syntax. Remaps apply on the next start.

### Rewind (double-Esc, full-screen TUI)

Double-Esc while idle opens a picker of the session's user turns
(`PhosonApp.handle_rewind`). Selecting one calls
`SessionController.jump_to_user_turn`, which moves `current_node_id` to
the node *before* the chosen turn — a generalization of `undo_last_turn`
(the "undone" messages stay in the tree as an abandoned branch; cost and
token metrics are cumulative and not rolled back, same contract as
`/undo`). The TUI then redraws the chat pane from the tree up to the new
cursor, pre-fills the composer with the selected turn's text, and pushes
the previous cursor onto `PhosonApp._rewind_stack` (consecutive rewinds
stack). `undo_jump` (`Ctrl+Z`) pops that stack in reverse order and
redraws forward.

### Model registry (`~/.phoson/models.json`)

Optional user-managed file (0600, created lazily) with two sections:

```jsonc
{
  // Model overrides. Keys are bare model ids. User values always win
  // over fetched data; models not in the provider's listing are appended
  // to the /model picker (local or custom models).
  "models": {
    "qwen3.8-27b": {
      "context_window": 262144,   // used for the prompt usage display
      "label": "Qwen 3.8 27B",    // optional display name
      "description": "local"      // optional
    }
  },
  // Non-sensitive provider settings. API keys never live here — they stay
  // in config.toml / env vars because this file may be synced or shared.
  "providers": {
    "openrouter": {
      "default_model": "qwen3.8-27b",        // picked on provider switch
      "base_url": "https://proxy.example/v1" // OpenAI-compatible override
    }
  }
}
```

Behavior:

- **Live listing:** `/model` and `/model list` always query every
  configured provider live (concurrently, active provider first) —
  **no model listing is persisted to disk**, so the picker never shows a
  stale list and works offline only in the sense that local providers
  (Ollama, vLLM, LM Studio) are queried locally.
- **Unavailable providers:** when a provider's live fetch fails
  (timeout, 401, rate limit…) it is shown explicitly as
  `⚠ <provider> — unavailable: <reason>` in the picker and in
  `/model list` — never a silent single-model fallback. The
  single-provider fallback still applies to `/subagent-model` and the
  inline `/model` autocomplete cache.
- **Unified picker:** a bare `/model` opens one picker spanning all
  configured providers — each row shows `id (provider)` — and selecting
  a model from another provider switches the `(provider, model)` pair
  together (persisted to `config.toml`).
- **OpenRouter ordering:** the OpenRouter listing is sorted by
  `benchmarks.artificial_analysis.agentic_index` (descending) so the
  strongest agentic/tool-use models come first; models without the
  field are listed last, alphabetically. The current model always
  stays on top.
- **Context window:** for the prompt usage display the resolution order
  is `models` override → engine registry. Files written by older
  versions may still carry a `cache` section, which
  `resolve_context_window` reads as a last-resort hint for the display
  only — the CLI never writes it anymore.
- **base_url:** honored by all OpenAI-compatible providers, OpenAI,
  OpenRouter and Anthropic (proxies / self-hosted gateways).
- The file is user-editable; invalid JSON never crashes the CLI (a warning
  is printed and defaults are used).

## REPL Usage

Start the REPL and type natural language or commands.

### Commands

| Command           | Description                              |
|-------------------|------------------------------------------|
| `/exit`, `/quit`  | Exit the CLI                            |
| `/new`            | Start a new session                     |
| `/clear`          | Alias for /new                          |
| `/model`          | Show, list, or switch model             |
| `/subagent-model` | Show or set sub-agent model             |
| `/env`            | Show environment variables              |
| `/cost`           | Show session cost breakdown             |
| `/tokens`         | Show token usage stats                  |
| `/steps`          | Show execution steps                    |
| `/update`         | Check for and install CLI updates (alias `/upgrade`) |
| `/tree`           | Show conversation tree                  |
| `/sessions`       | Interactive session picker               |
| `/delete`         | Delete a saved session                  |
| `/label`          | Label current node                      |
| `/keys`           | List key bindings and how to remap them |
| `/undo`           | Undo the last turn (branch before your last message) |
| `/attach`         | Attach image/audio/video/pdf           |
| `/attachments`    | List or clear attachments              |
| `/skills`         | List skills (`/skills <name>` shows one's instructions) |
| `/help`           | Show command reference                  |

### Slash Tab Completion

Type `/` and press Tab to see available commands.

## Built-in Tools

### FileTool

```python
from phoson_cli.tools import FileTool

tool = FileTool()
content = tool.read_file(path="/path/to/file", start_line=1, end_line=100)
tool.write_file(path="/path/to/file", content="new content")
tool.patch_file(
    path="/path/to/file",
    old_text="old content",
    new_text="new content",
)
entries = tool.list_dir(path="/path/to/dir")
```

### bash

The bash tool is an `AgentTool` (see `phoson_agent.tool.tool`). It runs a
single shell command, fully async (never blocks the event loop), and
returns `stdout + stderr` combined, capped at 50 KB.

```python
from phoson_cli.tools import bash

# Invoke it the way the agent does: handler(args, context=...)
result = await bash.handler(
    {"command": "ls -la"},
    context={"safe_mode": False, "bash_confirmation": None},
)
```

Parameters the model can set (JSON schema is built from the type
annotations):

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `command` | string | — | The shell command to run (required). |
| `timeout` | number | `30` | Hard timeout in seconds, **per invocation** (I-127). Raise it for long-running builds/tests/training — there is **no maximum**. Invalid values (`<=0`, non-numeric) fall back to 30 s with a note in the result. |

`safe_mode` and `bash_confirmation` are injected by the front end (not set
by the model): with `safe_mode=True` and no confirmation service available
(one-shot / scripts) the command **fails closed** rather than hanging.

### SearchTool

```python
from phoson_cli.tools import SearchTool

tool = SearchTool()
result = tool.run(args={"query": "python async", "source": "duckduckgo"})
```

### skill (Skills system)

Loads a skill's full instructions on demand (IMPROVEMENTS.md G5). A skill
is a directory with a `SKILL.md` file — frontmatter (`name`,
`description`) plus Markdown instructions, optionally next to bundled
`scripts/`/`references/`.

Two-tier progressive disclosure:

- **Index** — `skills.render_skill_index()` puts only `name: description`
  per skill into the *stable prefix* of the system prompt (cheap and
  constant, so the prompt cache still covers the whole prefix).
- **Body** — `skills.load_skill_body()` is returned by this tool as a
  normal *tool result*, so it lands in the conversation and never
  invalidates the cached prefix.

```python
from phoson_cli.skills import discover_skills, render_skill_index

skills = discover_skills()                 # project + global, deduped
index = render_skill_index(skills)         # goes in the system prompt
```

Search order (first match by name wins): `.phoson/skills/`,
`.agents/skills/`, `.claude/skills/` (repo root), then
`~/.phoson/skills/`. The tool is registered only when at least one skill
exists — `build_tools(include_skill=True|False)` overrides the
auto-detection.

### SubAgentTool

Two tools: `agent` (single task, clean context) and `agents` (multiple
tasks in parallel). Parallelism is bounded by `subagent_max_parallel`
(a semaphore — the parent agent decides how many tasks to spawn, not how
many LLM sessions may run at once), and each task is guarded by
`subagent_timeout_seconds`.

```python
from phoson_cli.tools.subagent import agent, agents

result = await agent.handler(
    {"task": "Search for info"},
    {"chat": chat, "available_tools": tools, "default_model": "gpt-4o",
     "max_iterations": 12, "safe_mode": False, "subagent_timeout_seconds": 300.0},
)
```

## Session Management

### SessionMetrics

Tracks accumulated metrics for the current session.

```python
@dataclass
class SessionMetrics:
    total_cost_usd: float
    total_credits: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_write_tokens: int
    total_cache_read_tokens: int
    step_count: int
    last_model: str
    steps: list[RunStep]
    phoson_weight: float
```

**Methods:**

- `add_run_step(step)` — Add step and update totals
- `reset()` — Reset all metrics for new session
- `load_from_meta(meta)` — Load from session metadata dict
- `to_meta()` — Convert to metadata dict for storage

**Properties:**

- `total_tokens` — Sum of input + output tokens
- `avg_cost_per_message` — Cost per step

## AttachmentManager

Manages multimodal file attachments.

```python
from phoson_cli.attachments import AttachmentManager, Attachment

manager = AttachmentManager()
manager.attach(path="/path/to/image.png")
manager.attach(path="/path/to/audio.mp3")
attachments = manager.list_pending()
manager.flush()  # Clear after sending
manager.clear()  # Clear without sending
```

## Renderer

Handles terminal rendering of agent events.

```python
from phoson_cli.renderer import Renderer

renderer = Renderer()
renderer.set_session(session_id)
renderer.flush_line()
renderer.finish_turn()
renderer.on_event(event)
renderer.start_waiting()
renderer.stop_waiting()
renderer.print_user_turn(text)
renderer.print_info(text)
renderer.print_warn(text)
renderer.print_error(text)
renderer.print_history(messages)
renderer.print_sessions_table(sessions)
renderer.print_help(commands)
```

## SubagentPanelTool

Shows live subagent metrics panel.

```python
from phoson_cli.tools.subagent_panel import SubagentPanelTool, SubagentMetrics

panel = SubagentPanelTool()
panel.render_panel(metrics=[])
panel.render_panel_frame(title="Subagents")
render_subagent_summary(metrics)
```

## Public API

```python
from phoson_cli import PhosonRepl
from phoson_cli.config import PhosonConfig, build_chat
from phoson_cli.commands import COMMANDS, parse_command
from phoson_cli.renderer import Renderer
from phoson_cli.attachments import AttachmentManager, Attachment
from phoson_cli.tools import build_tools, build_tools_dict
from phoson_cli.tools.base import BaseTool
from phoson_cli.tools.bash import BashTool
from phoson_cli.tools.files import FileTool
from phoson_cli.tools.search import SearchTool
from phoson_cli.tools.subagent import SubAgentTool
from phoson_cli.tools.subagent_panel import SubagentPanelTool, SubagentMetrics
from phoson_cli.repl import SessionMetrics
```