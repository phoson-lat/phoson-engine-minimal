"""Bash command execution tool.

The handler is fully async so it never blocks the event loop. The
interactive confirmation in ``safe_mode`` uses ``prompt_toolkit``'s
async prompt to stay cooperative with the running REPL.
"""

import asyncio

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from phoson_agent.tool import tool

MAX_BYTES = 50 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


async def _confirm_async(command: str) -> bool:
    """Ask the user (asynchronously) whether to run ``command``."""
    session: PromptSession[str] = PromptSession()
    try:
        with patch_stdout():
            answer = await session.prompt_async(
                f"Run bash command? {command!r} [y/N]: "
            )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


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
) -> str:
    """Execute a bash command and return ``stdout + stderr``.

    Args:
        command: The shell command to execute.
        safe_mode: When True, prompt the user for confirmation before
            running the command.
        timeout: Hard timeout in seconds. Defaults to 30s.
    """
    if safe_mode and not await _confirm_async(command):
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
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
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


@tool(inject=["safe_mode"])
async def bash(command: str, safe_mode: bool = False) -> str:
    """Execute a bash command and return stdout+stderr combined."""
    return await _run_bash(command, safe_mode=safe_mode)


# Backwards-compatible alias for tests and callers that imported the class.
class BashTool:
    """Thin shim around :func:`_run_bash` kept for backwards compatibility.

    New code should call ``bash`` (the registered tool) or ``_run_bash``
    directly. ``BashTool().run(...)`` exists so that tests written before
    the refactor keep working without modification.
    """

    async def run(
        self,
        command: str,
        safe_mode: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        return await _run_bash(command, safe_mode=safe_mode, timeout=timeout)
