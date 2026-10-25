"""User shell commands shared with the agent as context (``!`` shell mode).

A command the user runs from the composer's shell mode (or a ``!``-prefixed
line) is shown immediately as a bash card, and its output is *also* queued
on the controller: the next message the user sends carries one text block
per command, so the model sees what the user saw — without starting a turn
of its own when the command runs (the same gesture as Claude Code's ``!``).

The block is plain text with explicit delimiters so history replay can turn
it back into a bash card instead of dumping it into the user's message
line. UI-free: imported by the controller and by the replay renderer.
"""

from dataclasses import dataclass

SHELL_CONTEXT_HEADER = "[USER SHELL COMMAND]"

_PREAMBLE = (
    "The user ran this command themselves in their terminal; its output is "
    "shared with you as context."
)
_COMMAND_OPEN = "<command>\n"
_COMMAND_CLOSE = "\n</command>"
_OUTPUT_OPEN = "<output>\n"
_OUTPUT_CLOSE = "\n</output>"
_ERROR_OPEN = "<error>\n"
_ERROR_CLOSE = "\n</error>"


@dataclass(frozen=True)
class ShellContext:
    """One user-run shell command and what it produced."""

    command: str
    output: str
    error: str | None = None


def format_shell_context(entry: ShellContext) -> str:
    """Render *entry* as the model-visible text block."""
    if entry.error is not None:
        body = f"{_ERROR_OPEN}{entry.error}{_ERROR_CLOSE}"
    else:
        body = f"{_OUTPUT_OPEN}{entry.output or '(no output)'}{_OUTPUT_CLOSE}"
    return (
        f"{SHELL_CONTEXT_HEADER} {_PREAMBLE}\n"
        f"{_COMMAND_OPEN}{entry.command}{_COMMAND_CLOSE}\n"
        f"{body}"
    )


def parse_shell_context(text: str) -> ShellContext | None:
    """Inverse of :func:`format_shell_context`; ``None`` for any other text."""
    if not text.startswith(SHELL_CONTEXT_HEADER):
        return None
    start = text.find(_COMMAND_OPEN)
    end = text.find(_COMMAND_CLOSE, start + len(_COMMAND_OPEN))
    if start < 0 or end < 0:
        return None
    command = text[start + len(_COMMAND_OPEN) : end]
    rest = text[end + len(_COMMAND_CLOSE) :].lstrip("\n")
    for opener, closer, is_error in (
        (_OUTPUT_OPEN, _OUTPUT_CLOSE, False),
        (_ERROR_OPEN, _ERROR_CLOSE, True),
    ):
        # The body may itself contain the closing tag: take the *last* one.
        if rest.startswith(opener) and rest.endswith(closer):
            body = rest[len(opener) : len(rest) - len(closer)]
            if is_error:
                return ShellContext(command=command, output="", error=body)
            return ShellContext(
                command=command, output="" if body == "(no output)" else body
            )
    return None


__all__ = [
    "SHELL_CONTEXT_HEADER",
    "ShellContext",
    "format_shell_context",
    "parse_shell_context",
]
