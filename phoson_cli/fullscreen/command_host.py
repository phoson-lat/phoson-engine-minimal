"""Presentation host for CommandHandler in the full-screen front end.

Non-interactive effects (info/warn/error/help) go straight to the
sink's transcript. ``/provider``/``/sessions`` with no argument and the
simple yes/no ``confirm()`` (used by ``/update``) run as modal Floats
inside the running Application via
``PhosonApp.run_float_picker``/``run_float_confirm``. ``/model`` with no
argument does NOT open a Float — model selection is inline autocomplete
instead (cli_abel-style: type ``/model `` and pick from the dropdown fed
by ``PhosonApp.model_cache``, same mechanism as slash-command
completion), so a bare ``/model`` just points at that instead of
opening anything. The setup wizard (``/setup``) is not Float-hosted yet
— it needs to temporarily suspend full-screen mode to reuse the
existing prompt_toolkit installer unchanged, which is future work — so
it degrades to a clear notice instead of hanging or crashing. Commands
that take an explicit argument (``/model gpt-4o``, ``/provider openai
list``, ...) are unaffected since they never call into these methods.
"""

from typing import TYPE_CHECKING

from phoson_agent.sessions.models import SessionMeta

from ..models import ModelOption
from ..model_picker import ModelPickerResult
from ..session_picker import SessionPickerResult, build_session_picker
from ..provider_picker import ProviderPickerResult, build_provider_picker

if TYPE_CHECKING:
    from .app import PhosonApp


class FullScreenCommandHost:
    """``CommandHost`` implementation backed by the full-screen ``PhosonApp``."""

    def __init__(self, app: "PhosonApp") -> None:
        self.app = app

    def print_info(self, message: str) -> None:
        self.app.sink.notify("info", message)

    def print_warn(self, message: str) -> None:
        self.app.sink.notify("warn", message)

    def print_error(self, message: str) -> None:
        self.app.sink.notify("error", message)

    def print_help(self, entries: list[tuple[str, str]]) -> None:
        lines = "\n".join(f"{name:<16} {help_text}" for name, help_text in entries)
        self.app.sink.notify("info", lines)

    async def pick_model(
        self, models: list[ModelOption], current_model: str
    ) -> ModelPickerResult:
        self.print_info(
            "Type `/model <name>` — start typing for autocomplete suggestions "
            "(e.g. `/model claude`), or `/model list` to see all models."
        )
        return ModelPickerResult(cancelled=True)

    async def pick_provider(
        self, providers: list[str], current_provider: str
    ) -> ProviderPickerResult:
        picker = build_provider_picker(
            providers, current_provider, theme=self.app.theme
        )
        return await self.app.run_float_picker(picker)

    async def pick_session(
        self, sessions: list[SessionMeta], current_id: str
    ) -> SessionPickerResult:
        """Show the session picker, looping on multi-delete requests.

        ``X`` (delete marked) resolves the picker with ``delete_ids``
        instead of a selection; the deletes are applied here against
        storage and the picker **reopens with the fresh list** so several
        batches can be deleted without closing the window (#55). Destructive
        deletes always ask for confirmation first (B3) — a cancelled
        confirm deletes nothing and just reopens the picker.
        """
        remaining = list(sessions)
        while True:
            picker = build_session_picker(remaining, current_id, theme=self.app.theme)
            result = await self.app.run_float_picker(picker)
            if not result.delete_ids:
                return result
            ids = [sid for sid in result.delete_ids if sid != str(current_id)]
            if not ids:
                continue
            if not await self.app.run_float_confirm(
                f"Delete {len(ids)} session(s)? This cannot be undone."
            ):
                self.app.sink.notify("info", "Delete cancelled.")
                continue
            for sid in ids:
                await self.app.repl.storage.delete(sid)
                remaining = [s for s in remaining if str(s.id) != sid]
            self.app.sink.notify("info", f"Deleted {len(ids)} session(s).")

    async def confirm(self, prompt: str) -> bool:
        return await self.app.run_float_confirm(prompt)

    async def run_setup(self) -> None:
        self.print_info(
            "The setup wizard isn't available inside this UI yet — "
            "exit and run `phoson-cli --setup` instead."
        )


__all__ = ["FullScreenCommandHost"]
