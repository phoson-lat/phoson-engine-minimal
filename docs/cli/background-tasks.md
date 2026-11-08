# Background tasks

The agent can hand work to **sub-agents that run in the background** and keep
working while they do. Each one gets a task number such as `#3`.

## Starting one

The `agent` and `agents` tools take `background: true`:

- **Without it** the call blocks, as before: the agent waits for the
  sub-agent's answer.
- **With it** the call returns at once, for example "Started in the
  background: #3 audit the auth module", and the sub-agent keeps running.
  Starting one does not stop the turn, and cancelling the turn (Esc / Ctrl+C)
  does not stop it either.

The model chooses when to use it, typically for long, independent work it
doesn't need right away. You can also ask for it: "investigate X in the
background while you fix Y".

## Commands in the background

`bash` takes `background: true` too, for servers, watchers and long
builds or tests:

- **Return:** the call comes back at once with a task number.
- **Output:** the full output goes to a private log file under
  `~/.phoson/tasks/`, and the result delivered to the agent is the exit code
  plus the last 200 lines.
- **Time limit:** 1 hour, or the call's `timeout` if larger.
- **Permissions:** the command goes through the same permission gate as a
  foreground `bash`.

This replaces `run_bg_job`, which is deprecated. Keep using `run_bg_job` only
for a job that must outlive the CLI session.

## Sending a running call there (Ctrl+B)

In the full-screen TUI, `Ctrl+B` moves the running `bash`, sub-agent or
`web_fetch` calls to the background:

- The agent gets a placeholder result ("moved to the background as task #4")
  and keeps going.
- The call keeps running, and a `bash` command is no longer killed at its
  short timeout.
- MCP tools cannot be moved yet: their connection must stay in the task that
  opened it.

With nothing to move, `Ctrl+B` lists the tasks.

## Getting the results

When a background task finishes, its result reaches the agent once, through
whichever of these comes first:

- **The agent is working:** the result is added to the conversation before
  its next model call, so it never has to poll.
- **The agent is idle:** a turn starts on its own with the result, like a
  monitor wake.
- **The agent asks for it:** it can call one of the tools below.

| Tool | What it does |
|---|---|
| `task_status` | lists tasks with state, elapsed time, tokens and cost |
| `task_wait` | waits for some or all tasks to finish (up to 30 min) and returns their results |
| `task_output` | returns one task's full result, or its progress while running |
| `task_cancel` | stops a running task |

Results are framed as task output, not as messages from you, and long results
are clipped inline (`task_output` returns the full text).

## Seeing and managing tasks

**Full-screen TUI:**

- **Dock:** a few lines above the composer list the running tasks with their
  elapsed time and live detail (the current output line of a command, for
  example). Tasks waiting for your approval appear there too, and finished
  tasks stay for a few seconds.
- **Notices in the chat:** a line appears when a task finishes, fails or needs
  approval.
- **Ctrl+B view:** with nothing running that can be sent to the background,
  `Ctrl+B` opens the tasks view:
  - `Enter` shows a task's output;
  - `x` cancels it;
  - `a` / `d` approve or deny its pending permission question.
- **Footer hint:** while a `bash`, sub-agent or `web_fetch` call runs, the
  footer shows `ctrl+b background`.
- **Quitting:** `Ctrl+Q` with tasks still running asks for a second press,
  because those tasks will be cancelled.

**Both front ends** have `/tasks`, which also works while a turn runs:

| Command | Effect |
|---|---|
| `/tasks` | list tasks, including pending approvals |
| `/tasks 3` | show task #3's output (or progress) |
| `/tasks cancel 3` | stop it |
| `/tasks approve 3` / `/tasks deny 3` | answer its permission question |

### Approvals

When a background sub-agent needs a permission you have set to `ask`, no
dialog interrupts you. The question waits in the task list, and the sub-agent
pauses until you answer from the dock / Ctrl+B view or with
`/tasks approve|deny <n>`.

## Limits and lifetime

- **How many at once:** up to `max(4, 2 × subagent_max_parallel)` background
  tasks run at the same time; starting more fails with a clear message.
- **Timeout:** each sub-agent has the usual timeout (`subagent_timeout_seconds`,
  or the call's `timeout`).
- **Permissions:** background sub-agents use the same permission gate as the
  main agent.
- **Lifetime:** tasks live as long as the session. Quitting cancels the ones
  still running, and delivered results stay in the conversation, so `/resume`
  keeps them.
- **One-shot mode** (`-p`) has no background tasks. There, `background: true`
  runs the sub-agent or command in the foreground.
