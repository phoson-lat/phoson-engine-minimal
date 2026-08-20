# phoson_cli

Interactive command-line interface for the Phoson autonomous-agent platform.

## Overview

`phoson_cli` provides an interactive REPL with:

- **Streaming** — Real-time token-by-token output
- **Branching sessions** — Navigate non-linear conversation history
- **Tool execution** — Built-in file, bash, and search tools
- **Subagent support** — Run parallel agent tasks
- **Attachment support** — Images, audio, video, documents

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