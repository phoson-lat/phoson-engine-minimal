# Code mode (`run_code`)

With code mode on, the agent gets one more tool: **`run_code`**. Instead of
calling tools one model turn at a time, the agent writes a short Python
script that calls them as functions, loops, filters and aggregates. Only
what the script prints comes back into the conversation.

```python
# "Which of our Python files still import requests?"
paths = tools.glob(pattern="**/*.py").splitlines()
texts = parallel([lambda p=p: tools.read_file(path=p) for p in paths])
hits = [p for p, text in zip(paths, texts) if "import requests" in text]
print(len(hits), "of", len(paths), "files:", hits[:20])
```

Without code mode this takes one model turn per file, and every file's
contents land in the context window. With it, it takes one turn, and only
the final line comes back.

It works with every provider: `run_code` is an ordinary function tool. It
is not Anthropic's server-side programmatic tool calling, though it is the
same idea (Cloudflare calls theirs "Code Mode").

## Turning it on

Code mode is off by default.

| `config.toml` (`[defaults]`) | Environment                | Default  |                                                                                         |
| ---------------------------- | -------------------------- | -------- | --------------------------------------------------------------------------------------- |
| `code_mode`                  | `PHOSON_CODE_MODE`         | `false`  | Adds the `run_code` tool.                                                               |
| `code_mode_sandbox`          | `PHOSON_CODE_MODE_SANDBOX` | `"auto"` | `auto`, `required` or `off`; see [Sandbox](#sandbox).                                   |
| `code_mode_timeout`          | `PHOSON_CODE_MODE_TIMEOUT` | `120`    | Default time limit of one script, in seconds. The model can raise it per call, up to 30 min. |

When code mode is on, the system prompt tells the model to prefer
`run_code` for work that takes many tool calls, and to keep using direct
tool calls for single actions.

## What a script can use

| Name                          | What it does                                                                                     |
| ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `tools.<name>(...)`           | Calls a tool with the arguments of its schema. Positional arguments follow the schema order, required ones first. |
| `tools.call("name", **args)`  | Calls any tool by its registered name, MCP and plugin tools included.                           |
| `tools.search("keyword")`     | Finds tools: `[{name, py, signature, description}]`.                                             |
| `tools.help("name")`          | Full description plus parameter and result schemas.                                              |
| `tools.list()`                | Names of every tool the script can call.                                                         |
| `parallel(f1, f2, ...)`       | Runs zero-argument callables at the same time and returns their results in order.               |
| `ToolError`                   | Raised when a call fails or is refused; catch it to carry on.                                    |

What a call returns:

- **Structured data stays structured.** A tool that returns a dict gives
  the script a dict, and a result whose text is a JSON object or array is
  decoded.
- **MCP tools** return their `structuredContent` when the server provides
  it, otherwise their text content (decoded when it is JSON). An MCP
  `isError` result raises `ToolError`.
- Anything else comes back as a string, exactly the text the model would
  see. The built-in file tools are text: `read_file` numbers every line
  (`N<TAB>line`), and `glob`, `grep` and `list_dir` put one entry per line,
  so use `.splitlines()`.

Calls are synchronous (no `await`). Scripts can use only the standard
library.

The script cannot call these tools: `run_code` itself, `discover`,
`compact_context`, and the sub-agent tools `agent` and `agents`.

Limits per `run_code` call:

- **Time:** the time limit only counts time spent running the script.
  Time inside tool calls, including waiting for you at a permission
  prompt, does not count.
- **Tool calls:** at most 200.
- **Output:** printed output is capped at 50 KB.

When the script raises an exception, the model gets the traceback
(`File "<run_code>", line N`) and the result is marked as an error.

## Permissions

There are two layers:

1. **`run_code` itself** asks by default, like `bash`. With no rule of its
   own it follows `bash`'s rule: allowing `bash` allows it, and denying
   `bash` denies it. The prompt shows the **whole script**. Pressing
   **[a] Always** allows `run_code` from then on.
2. **Every tool call the script makes** goes through the same permission
   gate as a call the model makes directly: allow, ask and deny rules,
   allow-patterns and the guardian classifier. If a call needs approval,
   the script waits while you decide. A refused call raises `ToolError` in
   the script. Result-side middleware (output offloading, doom-loop error
   counting) applies to the `run_code` result, not to each call inside it,
   because those results go to the script, not into the conversation.

The calls a script makes don't appear one by one in the transcript. The
`run_code` result ends with a summary line such as
`[run_code: 12 tool calls — read_file×10, grep×2; 1 failed]`.

## Sandbox

The script runs in a separate Python process. That process gets a minimal
environment: `PATH`, `HOME`, locale and temp variables, and **no API keys
or tokens**. Tools that need credentials run inside Phoson, not in the
script.

Where the operating system allows it, the process is also sandboxed:

| Platform | Backend                     | Policy                                                                                                       |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------------------------ |
| macOS    | `sandbox-exec` (built in)   | No network. No file writes outside a private temp directory. No reads of `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`, `~/.phoson`, gh/gcloud/docker/kube credentials. |
| Linux    | `bwrap` (install `bubblewrap`) | Same policy. Requires unprivileged user namespaces, which are checked once.                                |
| Windows  | none                        | —                                                                                                            |

With the sandbox on, the script can only change the outside world through
`tools.*`, so every side effect passes the permission gate.

`code_mode_sandbox` controls it:

- **`auto`** uses the sandbox when one is available. Otherwise the script
  runs unsandboxed and the result says so.
- **`required`** refuses to run without a sandbox.
- **`off`** never sandboxes.

## Which Python runs the script

- **Installed from PyPI or a source checkout:** the interpreter running
  Phoson.
- **Standalone binary:** a `python3` (or `python`) found on `PATH`. If there
  is none, the binary runs the script itself with its bundled standard
  library, which may lack some modules.

`phoson-cli --self-test` checks both paths.

## For library users

```python
from phoson_agent import AgentEngine
from phoson_agent.code_mode import CodeModeConfig

engine = AgentEngine(
    chat=chat,
    tools=[...],
    code_mode=CodeModeConfig(timeout_seconds=60, sandbox="auto"),
)
```

You can tune individual tools through `AgentTool.metadata`
(`phoson_agent.models`):

| Key                 | Effect                                                                         |
| ------------------- | ------------------------------------------------------------------------------ |
| `CODE_MODE_KEY`     | `False` keeps the tool out of scripts.                                         |
| `CODE_RESULT_KEY`   | Callable `raw -> value` that shapes what a script receives (it may raise).     |
| `OUTPUT_SCHEMA_KEY` | JSON Schema of the result, shown by `tools.help()`.                            |
