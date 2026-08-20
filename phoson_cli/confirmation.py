"""Classic (prompt_toolkit) implementation of ConfirmationService.

Textual migration (phase 2): the safe-mode bash confirmation used to
live inside the bash tool, creating its own ``PromptSession``. The tool
now receives a :class:`~phoson_cli.ui_protocols.ConfirmationService`
through engine context injection; this module is the classic front end's
implementation. The Textual TUI will inject a modal-based service
instead — no tool change required.
"""

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout


class PromptToolkitConfirmationService:
    """Interactive confirmations via prompt_toolkit (classic REPL)."""

    async def confirm_bash(self, command: str) -> bool:
        """Ask the user (asynchronously) whether to run ``command``."""
        session: PromptSession[str] = PromptSession()
        try:
            with patch_stdout():
                answer = await session.prompt_async(
                    f"Run bash command? {command!r} [y/N]:"
                )
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}
