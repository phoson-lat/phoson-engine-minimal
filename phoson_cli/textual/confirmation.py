"""Textual implementation of :class:`ConfirmationService` (phase 3).

Safe-mode bash asks the app to show the
:class:`~phoson_cli.textual.dialogs.BashConfirmation` modal and awaits
the answer — a real interactive confirmation inside the TUI (the
classic REPL uses prompt_toolkit instead).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import PhosonTextualApp


class TextualConfirmationService:
    """Answers confirmation prompts through the TUI modal."""

    def __init__(self, app: "PhosonTextualApp") -> None:
        self._app = app

    async def confirm_bash(self, command: str) -> bool:
        return await self._app.ask_confirmation(command)
