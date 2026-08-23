"""Full-screen (Float-based) implementation of ConfirmationService.

The bash tool's safe-mode confirmation can fire mid-stream, from deep
inside the same background task driving the turn's ``run_turn`` — the
confirmation Float renders on top of the still-live chat pane (which
keeps invalidating from other events) while that task awaits an answer.
Reuses the same Float mechanism as the command pickers
(:meth:`~phoson_cli.fullscreen.app.PhosonApp.run_float_confirm`),
including its Ctrl+C → "no" resolution, so cancelling a run that is
mid-confirmation can't leave the tool call hanging forever.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import PhosonApp


class FullScreenConfirmationService:
    """Interactive confirmations via a modal Float (full-screen front end)."""

    def __init__(self, app: "PhosonApp") -> None:
        self.app = app

    async def confirm_bash(self, command: str) -> bool:
        """Ask whether ``command`` may run."""
        return await self.app.run_float_confirm(f"Run bash command? {command!r}")


__all__ = ["FullScreenConfirmationService"]
