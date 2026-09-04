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

import asyncio
from typing import Annotated

from phoson_agent.tool import tool

from ._timeouts import sanitize_timeout
from ..ui_protocols import ConfirmationService

MAX_BYTES = 50 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0

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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return f"Failed to spawn shell: {exc}"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        finally:
            try:
                await proc.communicate()
            except Exception:  # noqa: BLE001
                pass
        return f"Command timed out after {timeout:.0f}s"

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    return _truncate(stdout + stderr)


@tool(inject=["safe_mode", "bash_confirmation"])
async def bash(
    command: str,
    timeout: Annotated[float, TIMEOUT_DESCRIPTION] = DEFAULT_TIMEOUT_SECONDS,
    safe_mode: bool = False,
    bash_confirmation: ConfirmationService | None = None,
) -> str:
    """Execute a bash command and return stdout+stderr combined.

    Use for anything the dedicated tools don't cover: running builds/tests,
    git, and — because there is no native search tool — `grep -rn` / `rg` to
    search across files and `find`/`ls` for deep or filtered listing. Prefer
    read_file/patch_file over cat/sed for reading and editing, and keep
    commands non-interactive (no prompts, no TUIs). Chained commands
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
    result = await _run_bash(
        command, safe_mode=safe_mode, timeout=value, confirmation=bash_confirmation
    )
    return f"{note}\n{result}" if note else result
