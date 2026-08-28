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

from typing import TYPE_CHECKING, cast

from phoson_agent.sessions.models import SessionMeta

from ..theme import Theme
from ..models import ModelOption
from ..command_host import HelpEntry, HelpEntries, is_grouped_help
from ..model_picker import ModelPickerResult
from ..theme_picker import ThemePickerResult, build_theme_picker
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

    def print_help(self, entries: HelpEntries) -> None:
        """Render ``/help`` into the chat pane, grouped by category (C4).

        Accepts the grouped form ``[(category, [(name, desc), ...]), ...]``
        or a flat ``(name, help)`` list for backward compatibility.
        """
        lines: list[str] = []
        if is_grouped_help(entries):
            for category, commands in entries:  # type: ignore[union-attr]
                lines.append(category)
                lines.extend(
                    f"  {name:<18} {help_text}" for name, help_text in commands
                )
        else:
            flat = cast("list[HelpEntry]", entries)
            lines.extend(f"{name:<16} {help_text}" for name, help_text in flat)
        self.app.sink.notify("info", "\n".join(lines))

    def print_renderable(self, renderable: object) -> None:
        """Print a Rich renderable into the chat pane.

        The sink stores *renderables* (not strings) and the ANSI bridge
        renders them per width — so the object goes in as-is.
        """
        self.app.sink.blocks.append(renderable)
        self.app.sink.dirty = True
        self.app.app.invalidate()

    async def pick_model(
        self,
        models: list[ModelOption],
        current_model: str,
        *,
        unavailable: list[tuple[str, str]] | None = None,
    ) -> ModelPickerResult:
        """Open the unified multi-provider model picker as a Float (I-113).

        Bare ``/model`` now opens the same unified view the classic REPL
        gets (all configured providers in one list, each row tagged with
        its provider), instead of only pointing at inline autocomplete.
        Inline autocomplete (type ``/model <name>``) is still available
        and untouched — this just gives a bare ``/model`` a real picker.
        """
        if not models:
            self.print_info("No models available.")
            return ModelPickerResult(cancelled=True)
        from ..model_picker import build_unified_model_picker

        picker = build_unified_model_picker(
            models,
            current_model,
            current_provider=self.app.repl.config.provider,
            page_size=12,
            theme=self.app.theme,
        )
        return await self.app.run_float_picker(picker)

    async def pick_provider(
        self, providers: list[str], current_provider: str
    ) -> ProviderPickerResult:
        picker = build_provider_picker(
            providers, current_provider, theme=self.app.theme
        )
        return await self.app.run_float_picker(picker)

    async def pick_theme(
        self,
        current_theme: str,
        *,
        detected_theme: str | None = None,
    ) -> ThemePickerResult:
        """Host the theme picker as a modal Float (E4)."""
        picker = build_theme_picker(
            current_theme,
            theme=self.app.theme,
            detected_name=detected_theme,
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

    def apply_theme(self, theme: Theme) -> None:
        """Full-screen front end: re-color the whole application (E4)."""
        self.app.apply_theme(theme)

    async def run_setup(self) -> None:
        self.print_info(
            "The setup wizard isn't available inside this UI yet — "
            "exit and run `phoson-cli --setup` instead."
        )


__all__ = ["FullScreenCommandHost"]
