"""Classic (prompt_toolkit) implementation of ConfirmationService.

The safe-mode bash confirmation used to live inside the bash tool,
creating its own ``PromptSession``. The tool now receives a
:class:`~phoson_cli.ui_protocols.ConfirmationService` through engine
context injection; this module is the classic front end's
implementation. A full-screen front end can inject a modal-based
service instead — no tool change required.
"""

from collections.abc import Sequence

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from phoson_agent import Choice, FormField


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

    async def select_plugin(
        self, title: str, message: str, choices: Sequence[Choice]
    ) -> str | None:
        """Ask for a numbered plugin choice; EOF/cancel safely returns None."""
        if not choices:
            return None
        session: PromptSession[str] = PromptSession()
        lines = [title, message]
        lines.extend(
            f"  {index}. {choice.label}"
            + (f" — {choice.detail}" if choice.detail else "")
            for index, choice in enumerate(choices, start=1)
        )
        try:
            with patch_stdout():
                answer = await session.prompt_async(
                    "\n".join(lines) + "\nSelect [Esc]: "
                )
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            selected = int(answer.strip())
        except ValueError:
            return None
        return choices[selected - 1].id if 1 <= selected <= len(choices) else None

    async def form_plugin(
        self, title: str, fields: Sequence[FormField]
    ) -> dict[str, str] | None:
        """Collect a small sequential form without exposing UI widgets to plugins."""
        session: PromptSession[str] = PromptSession()
        values: dict[str, str] = {}
        try:
            with patch_stdout():
                for index, field in enumerate(fields):
                    prefix = title if index == 0 else ""
                    default = f" [{field.default}]" if field.default is not None else ""
                    value = await session.prompt_async(
                        f"{prefix}\n{field.label}{default}: ",
                        is_password=field.kind == "password",
                    )
                    value = value.strip() or (field.default or "")
                    if field.required and not value:
                        return None
                    if field.kind == "integer" and value:
                        int(value)
                    values[field.id] = value
        except (EOFError, KeyboardInterrupt, ValueError):
            return None
        return values
