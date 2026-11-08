"""Bash command execution tool.

The handler is fully async so it never blocks the event loop.

Safe-mode confirmation: the tool no longer opens its own prompt. It
receives an optional :class:`~phoson_cli.ui_protocols.ConfirmationService`
through engine context injection (``bash_confirmation``):

- classic REPL: a prompt_toolkit-based service (interactive prompt);
- full-screen front end: a modal-based service;
- nothing injected (one-shot / scripts): **fail closed** — the command
  is refused with an actionable message instead of hanging or running.
"""

import os
import time
import asyncio
from typing import Any, Annotated
from pathlib import Path
from collections import deque

from phoson_agent.tool import tool
from phoson_agent.detach import detach_requested
from phoson_agent.procutil import session_kwargs, kill_process_tree

from ._timeouts import sanitize_timeout
from ..ui_protocols import ConfirmationService

MAX_BYTES = 50 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Time limit for a command running in the background (``background: true``
#: or sent there with Ctrl+B) unless the call asked for a longer one.
BACKGROUND_TIMEOUT_SECONDS = 3600.0
#: Lines of a background command's output returned as its result.
BACKGROUND_TAIL_LINES = 200

BACKGROUND_DESCRIPTION = (
    "Run in the background and return at once with a task id such as #4: "
    "for servers, watchers and long builds/tests you don't need to wait on. "
    "The full output goes to a log file; the last lines are delivered to you "
    "when it exits (task_output / task_wait to check sooner). Time limit: "
    "1 hour, or `timeout` if larger."
)

#: Description the LLM sees for the ``timeout`` parameter. The ``@tool``
#: schema is built from the type annotations (not the docstring), so this
#: ``Annotated`` string is how the parameter gets documented for the model.
TIMEOUT_DESCRIPTION = (
    "Hard timeout in seconds. Defaults to 30. Raise for long-running "
    "builds/tests/training (no maximum). Make commands that would wait "
    "for interactive input non-interactive first."
)


def _truncate(output: str) -> str:
    """Cap output at ``MAX_BYTES`` while keeping it valid UTF-8."""
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_BYTES:
        return output
    clipped = encoded[:MAX_BYTES].decode("utf-8", errors="replace")
    return f"{clipped}\n\n[...truncated]"


async def _read_capped_stream(
    stream: asyncio.StreamReader,
    max_bytes: int = MAX_BYTES,
) -> tuple[bytes, bool]:
    """Read from ``stream`` up to ``max_bytes``, draining remainder to EOF.

    Returns:
        A tuple of (captured_bytes, was_truncated).
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False

    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if total < max_bytes:
            remaining = max_bytes - total
            if len(chunk) <= remaining:
                chunks.append(chunk)
                total += len(chunk)
            else:
                chunks.append(chunk[:remaining])
                total += remaining
                truncated = True
        else:
            truncated = True

    return b"".join(chunks), truncated


async def _terminate_process_group(
    proc: asyncio.subprocess.Process,
    stream_tasks: tuple[asyncio.Task, asyncio.Task],
) -> None:
    """Kill the isolated process group, reap the shell, and clean pipe readers.

    ``kill_process_tree`` picks the platform primitive (``SIGKILL`` to the
    group on POSIX, ``taskkill /T /F`` on Windows) and never raises; the
    direct-child ``proc.kill()`` afterwards is the belt-and-braces for the
    case where the tree kill could not run at all.

    The tree kill is skipped once the child has been reaped
    (``proc.returncode is not None``): ``kill_process_tree`` kills *by pid*,
    and a pid whose process is gone may already have been recycled onto an
    unrelated one — on Windows that is the ``taskkill /T /F`` bluescreen path.
    Nothing is lost on POSIX, where ``os.getpgid`` on a dead pid raises anyway,
    and ``proc.kill()`` below keeps working because it goes through the open
    process handle rather than the number.
    """
    if proc.returncode is None:
        kill_process_tree(proc.pid)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    finally:
        for task in stream_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*stream_tasks, return_exceptions=True)


async def _await_process_cleanup(
    proc: asyncio.subprocess.Process,
    stream_tasks: tuple[asyncio.Task, asyncio.Task],
) -> None:
    """Finish process cleanup even if more cancellation arrives while awaiting."""
    cleanup = asyncio.create_task(_terminate_process_group(proc, stream_tasks))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    await cleanup


async def _run_bash(
    command: str,
    safe_mode: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    confirmation: ConfirmationService | None = None,
) -> str:
    """Execute a bash command and return ``stdout + stderr``.

    Args:
        command: The shell command to execute.
        safe_mode: When True, confirm with the user before running.
        timeout: Hard timeout in seconds. Defaults to 30s.
        confirmation: Interactive confirmation service (injected by the
            front end). Required when safe_mode is on; without it the
            command is refused (fail closed).
    """
    if safe_mode:
        if confirmation is None:
            return (
                "Blocked: safe_mode is enabled but no interactive "
                "confirmation is available in this context. Run the CLI "
                "interactively or disable safe_mode."
            )
        if not await confirmation.confirm_bash(command):
            return "Cancelled by user (safe_mode enabled)."

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **session_kwargs(),
        )
    except OSError as exc:
        return f"Failed to spawn shell: {exc}"

    if proc.stdout is None or proc.stderr is None:
        return "Failed to capture subprocess streams"

    stdout_task = asyncio.create_task(_read_capped_stream(proc.stdout, MAX_BYTES))
    stderr_task = asyncio.create_task(_read_capped_stream(proc.stderr, MAX_BYTES))
    stream_tasks = (stdout_task, stderr_task)

    async def _capture() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
        out_res, err_res = await asyncio.gather(stdout_task, stderr_task)
        await proc.wait()
        return out_res, err_res

    try:
        (stdout_b, stdout_trunc), (stderr_b, stderr_trunc) = await _wait_capture(
            _capture(), timeout
        )
    except TimeoutError:
        await _await_process_cleanup(proc, stream_tasks)
        return f"Command timed out after {timeout:.0f}s"
    except asyncio.CancelledError:
        await _await_process_cleanup(proc, stream_tasks)
        raise

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    combined = stdout + stderr

    if stdout_trunc or stderr_trunc:
        # If capped during stream reading, ensure it has the truncated suffix
        if len(combined.encode("utf-8", errors="replace")) > MAX_BYTES:
            return _truncate(combined)
        return f"{combined}\n\n[...truncated]"

    return _truncate(combined)


async def _wait_capture(capture: Any, timeout: float) -> Any:
    """Await the command, extending the limit if it is sent to the background.

    A foreground call keeps its short ``timeout``. When the user sends the
    call to the background (Ctrl+B), the command must not be killed at that
    deadline: it gets :data:`BACKGROUND_TIMEOUT_SECONDS` (or more) instead.
    """
    detach = detach_requested()
    if detach is None:
        return await asyncio.wait_for(capture, timeout=timeout)
    work = asyncio.ensure_future(capture)
    detached = asyncio.ensure_future(detach.wait())
    try:
        await asyncio.wait(
            {work, detached}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if work.done():
            return work.result()
        if detach.is_set():
            return await asyncio.wait_for(
                work, timeout=max(timeout, BACKGROUND_TIMEOUT_SECONDS)
            )
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise TimeoutError
    except asyncio.CancelledError:
        work.cancel()
        raise
    finally:
        detached.cancel()


def _background_log_dir() -> Path:
    base = Path(os.environ.get("PHOSON_HOME", "~/.phoson")).expanduser() / "tasks"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


async def _run_background(command: str, record: Any, limit: float) -> str:
    """Run ``command`` for a background task; stream its output to a log file."""
    log_path = (
        _background_log_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{record.id}.log"
    )
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    tail: deque[str] = deque(maxlen=BACKGROUND_TAIL_LINES)
    with os.fdopen(fd, "wb") as log:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **session_kwargs(),
        )
        assert proc.stdout is not None
        record.detail = f"pid {proc.pid}"

        async def _pump() -> None:
            assert proc.stdout is not None
            last_note = 0.0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                log.write(line)
                text = line.decode("utf-8", errors="replace").rstrip()
                tail.append(text)
                now = time.monotonic()
                if text.strip() and now - last_note > 0.5:
                    record.detail = text.strip()[:120]
                    last_note = now
                    notify = getattr(record, "_notify", None)
                    if callable(notify):
                        notify()
            log.flush()

        pump = asyncio.create_task(_pump())
        try:
            await asyncio.wait_for(asyncio.gather(pump, proc.wait()), timeout=limit)
        except TimeoutError:
            await _await_process_cleanup(proc, (pump, pump))
            return (
                f"Timed out after {limit:.0f}s and was stopped.\n"
                + "\n".join(tail)
                + f"\n[full log: {log_path}]"
            )
        except asyncio.CancelledError:
            await _await_process_cleanup(proc, (pump, pump))
            raise
    shown = "\n".join(tail)
    return f"Exit code {proc.returncode}.\n{shown}\n[full log: {log_path}]"


@tool(
    inject=["safe_mode", "bash_confirmation", "task_manager"],
    detachable=True,
)
async def bash(
    command: str,
    timeout: Annotated[float, TIMEOUT_DESCRIPTION] = DEFAULT_TIMEOUT_SECONDS,
    background: Annotated[bool, BACKGROUND_DESCRIPTION] = False,
    *,
    safe_mode: bool = False,
    bash_confirmation: ConfirmationService | None = None,
    task_manager: Any = None,
) -> str:
    """Execute a bash command and return stdout+stderr combined.

    Use for anything the dedicated tools don't cover: running builds/tests,
    git, and shell plumbing that no tool exposes. Prefer the dedicated
    tools over their shell equivalents — read_file/patch_file over cat/sed,
    the `grep`/`glob` tools over `grep -rn`/`find` — and keep commands
    non-interactive (no prompts, no TUIs). Chained commands
    (`;`, `&&`, `|`) and command substitution never match an allow-pattern,
    so they always go through confirmation when bash is on ask/deny.

    Args:
        command: The shell command to execute.
        timeout: Hard timeout in seconds (per-invocation override).
            Defaults to 30s; raise it for long-running builds, tests or
            training jobs (no upper bound). Invalid values fall back to
            the default with a note in the result.
        safe_mode: When True, confirm with the user before running
            (injected by the front end, not set by the model).
        bash_confirmation: Confirmation service injected by the front end.
    """
    value, note = sanitize_timeout(timeout, DEFAULT_TIMEOUT_SECONDS)
    if background and task_manager is not None:
        if safe_mode:
            if bash_confirmation is None:
                return "Blocked: safe_mode is enabled and cannot confirm here."
            if not await bash_confirmation.confirm_bash(command):
                return "Cancelled by user (safe_mode enabled)."
        limit = max(value, BACKGROUND_TIMEOUT_SECONDS)

        async def _run(record: Any) -> str:
            record._notify = lambda: task_manager.notify(record)
            return await _run_background(command, record, limit)

        try:
            record = task_manager.start("bash", command, _run)
        except RuntimeError as exc:
            return f"Not started: {exc}"
        return (
            f"Started in the background as {record.label}: {command}\n"
            "Keep working; the exit code and last output lines are delivered "
            "to you when it exits (task_output / task_wait to check sooner)."
        )
    result = await _run_bash(
        command, safe_mode=safe_mode, timeout=value, confirmation=bash_confirmation
    )
    return f"{note}\n{result}" if note else result
