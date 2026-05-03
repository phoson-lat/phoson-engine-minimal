"""Bash command execution tool."""

import subprocess

from phoson_agent.tool import tool

from .base import BaseTool

MAX_BYTES = 50 * 1024


class BashTool(BaseTool):
    """Tool to execute shell commands."""

    def run(self, command: str, safe_mode: bool = False) -> str:
        """Execute a bash command and return stdout+stderr combined."""
        if safe_mode:
            answer = input(f"Run bash command? {command!r} [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                return "Cancelled by user (safe_mode enabled)."

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "Command timed out after 30s"

        output = (result.stdout or "") + (result.stderr or "")
        if len(output.encode("utf-8", errors="replace")) > MAX_BYTES:
            clipped = output.encode("utf-8", errors="replace")[:MAX_BYTES].decode(
                "utf-8", errors="replace"
            )
            return clipped + "\n\n[...truncated]"
        return output


@tool(inject=["safe_mode"])
def bash(command: str, safe_mode: bool = False) -> str:
    """Execute a bash command and return stdout+stderr combined."""
    return BashTool().run(command, safe_mode=safe_mode)
