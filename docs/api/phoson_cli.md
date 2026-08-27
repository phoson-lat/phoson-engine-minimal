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

- One line per **action** (the 16 defaults: `submit`, `newline`,
  `page_up`, `page_down`, `line_up`, `line_down`, `scroll_home`,
  `scroll_end`, `clear`, `toggle_reasoning`, `ctrl_d`, `paste_image`,
  `copy_mode`, `escape`, `undo_jump`, `exit`).
- Each value is a prompt_toolkit key sequence (`"c-x"`, `"f13"`,
  `"s-up"`, …); a chord is a space-separated string (`"c-x c-e"`).
- `""` unbinds the action; `[]` is rejected (use `""`).
- `copy_mode` (default `F2`) opens the keyboard copy mode (G3).
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

### Copy mode (full-screen TUI)

The full-screen app captures the terminal's mouse to support smooth
chat scroll-wheel (`mouse_support=True`), which prevents the terminal
from offering native click-drag text selection. Copy mode
(IMPROVEMENTS.md G3, issue #57) is the universal, keyboard-driven
solution:

- **Entry:** Press `F2` (the `copy_mode` action, remappable via `[keys]`)
  or type `/copy`. The anchor is placed at the top-left cell of the
  visible chat pane and the cursor at the bottom-right cell — so the
  entire visible page is selected immediately.
- **Navigation:**
  - `↑` / `↓` / `←` / `→`: Extend the cursor one character or line.
    `←` and `→` wrap across row boundaries naturally.
  - `Home` / `End`: Jump cursor to the start/end of the current row.
  - `PgUp` / `PgDn`: Jump cursor a full visible page.
  - The selected rows are highlighted in reverse video (`\x1b[7m`).
  - The bottom footer displays copy-mode key hints:
    `[↑/↓/←/→] Move  [PgUp/PgDn] Jump page  [Enter] Copy  [Esc] Cancel`.
- **Yank to clipboard:** Press `Enter` (or `Ctrl+Y`) to copy the
  selected text to the system clipboard via the appropriate platform
  tool (`wl-copy` on Wayland, `xclip` on X11, `pbcopy` on macOS) — the
  exact write counterpart of the Ctrl+V paste mechanism. A toast reports
  how many characters were copied.
- **Cancel:** Press `Esc` to exit copy mode without copying.
- **Mouse fallback:** On terminals that support mouse bypass (e.g.,
  holding `Shift` while dragging in GNOME Terminal, iTerm2, or Alacritty),
  native click-drag selection continues to work directly.

### Model registry (`~/.phoson/models.json`)

Optional user-managed file (0600, created lazily) with three sections:

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
  },
  // Cache (managed by the CLI, do not edit): provider model listings
  // with a fetch timestamp, refreshed automatically by /model.
  "cache": { "fetched_at": 1755700000.0, "providers": { "openrouter": [ ... ] } }
}
```

Behavior:

- **Instant picker:** while the cache is fresh (TTL 24 h) `/model` shows
  the cached listing without any network call — it works offline.
- **Offline fallback:** if a live fetch fails, the stale cache is shown
  (with a warning) instead of a single-model list.
- **Context window:** for the prompt usage display the resolution order is
  `models` override → cache → engine registry.
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

### BashTool

```python
from phoson_cli.tools import BashTool

tool = BashTool()
result = tool.run(args={"command": "ls -la"})
```

### SearchTool

```python
from phoson_cli.tools import SearchTool

tool = SearchTool()
result = tool.run(args={"query": "python async", "source": "duckduckgo"})
```

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