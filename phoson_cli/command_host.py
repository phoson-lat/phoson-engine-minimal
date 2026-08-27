"""Presentation port for :class:`~phoson_cli.commands.CommandHandler`.

The handler owns command *semantics* (what ``/model`` does). A host owns
how those effects are shown and how interactive choices are collected:

- Classic REPL: :class:`RendererCommandHost` (Rich + prompt_toolkit pickers).
- Full-screen front end: a Float/modal-based host.

Unit tests keep constructing ``CommandHandler(dummy_repl)`` — the
renderer-backed host is the default when none is injected.
"""

from typing import Any, Protocol, runtime_checkable

from phoson_agent.sessions.models import SessionMeta

from .theme import Theme
from .models import ModelOption
from .model_picker import ModelPickerResult
from .theme_picker import ThemePickerResult
from .session_picker import SessionPickerResult
from .provider_picker import ProviderPickerResult

#: One ``/help`` row: ``(display_name, description)``.
HelpEntry = tuple[str, str]
#: A categorized ``/help`` section: ``(category_title, rows)``.
HelpSection = tuple[str, list[HelpEntry]]
#: Either form ``print_help`` accepts (grouped since C4, flat legacy).
HelpEntries = list[HelpEntry] | list[HelpSection]


def is_grouped_help(entries: HelpEntries) -> bool:
    """True when *entries* uses the grouped ``(category, rows)`` form."""
    return bool(entries) and isinstance(entries[0][1], list)


@runtime_checkable
class CommandHost(Protocol):
    """UI adapter used by :class:`~phoson_cli.commands.CommandHandler`."""

    def print_info(self, message: str) -> None: ...

    def print_warn(self, message: str) -> None: ...

    def print_error(self, message: str) -> None: ...

    def print_help(self, entries: HelpEntries) -> None: ...

    def print_renderable(self, renderable: object) -> None:
        """Print a Rich renderable (e.g. the colored /tree). Optional."""
        ...

    async def pick_model(
        self, models: list[ModelOption], current_model: str
    ) -> ModelPickerResult: ...

    async def pick_provider(
        self, providers: list[str], current_provider: str
    ) -> ProviderPickerResult: ...

    async def pick_theme(
        self, current_theme: str, *, detected_theme: str | None = None
    ) -> ThemePickerResult: ...

    def apply_theme(self, theme: Theme) -> None:
        """Re-color the active front end after a /theme switch (E4).

        Host-specific because each front end owns a different set of
        theme consumers (classic: Rich renderer + prompt style;
        full-screen: plus the chat-pane style dict, sink and banner).
        """
        ...

    async def pick_session(
        self, sessions: list[SessionMeta], current_id: str
    ) -> SessionPickerResult: ...

    async def confirm(self, prompt: str) -> bool: ...

    async def run_setup(self) -> None: ...

    def start_copy_mode(self) -> None:
        """Enter the full-screen copy mode (IMPROVEMENTS.md G3).

        Host-specific: only the full-screen front end has a selectable chat
        pane, so the classic host degrades to a notice (the feature is
        TUI-only).
        """
        ...


class RendererCommandHost:
    """Classic host: Rich renderer + prompt_toolkit pickers.

    Picker calls go through ``phoson_cli.commands`` so existing unit
    tests can monkeypatch ``pick_model`` / ``pick_provider`` / ``save_config``.
    """

    def __init__(self, repl: Any) -> None:
        self.repl = repl

    def print_info(self, message: str) -> None:
        self.repl.renderer.print_info(message)

    def print_warn(self, message: str) -> None:
        renderer = self.repl.renderer
        printer = getattr(renderer, "print_warn", None)
        if printer is not None:
            printer(message)
        else:
            renderer.print_info(message)

    def print_error(self, message: str) -> None:
        self.repl.renderer.print_error(message)

    def print_help(self, entries: HelpEntries) -> None:
        """Render ``/help``; grouped (C4) via the renderer, flat inline."""
        renderer = self.repl.renderer
        printer = getattr(renderer, "print_help", None)
        if printer is not None:
            printer(entries)
            return
        if is_grouped_help(entries):
            for _title, commands in entries:  # type: ignore[union-attr]
                for name, help_text in commands:
                    renderer.print_info(f"{name}  {help_text}")
            return
        for name, help_text in entries:  # type: ignore[union-attr]
            renderer.print_info(f"{name}  {help_text}")

    def print_renderable(self, renderable: object) -> None:
        """Print a Rich renderable (e.g. the colored /tree)."""
        self.repl.renderer.console.print(renderable)

    async def pick_model(
        self, models: list[ModelOption], current_model: str
    ) -> ModelPickerResult:
        from phoson_cli import commands as commands_mod

        return await commands_mod.pick_model(
            models=models,
            current_model=current_model,
            theme=getattr(self.repl, "theme", None),
        )

    async def pick_provider(
        self, providers: list[str], current_provider: str
    ) -> ProviderPickerResult:
        from phoson_cli import commands as commands_mod

        return await commands_mod.pick_provider(
            providers=providers,
            current_provider=current_provider,
            theme=getattr(self.repl, "theme", None),
        )

    async def pick_theme(
        self, current_theme: str, *, detected_theme: str | None = None
    ) -> ThemePickerResult:
        from phoson_cli import commands as commands_mod

        return await commands_mod.pick_theme(
            current_theme,
            theme=getattr(self.repl, "theme", None),
            detected_name=detected_theme,
        )

    def apply_theme(self, theme: Theme) -> None:
        """Classic front end: re-point the shared renderer's theme."""
        self.repl.apply_theme(theme)

    async def pick_session(
        self, sessions: list[SessionMeta], current_id: str
    ) -> SessionPickerResult:
        from phoson_cli.session_picker import pick_session

        return await pick_session(
            sessions=sessions,
            current_id=current_id,
            page_size=15,
            theme=getattr(self.repl, "theme", None),
        )

    async def confirm(self, prompt: str) -> bool:
        from .updater import _update_confirm

        return await _update_confirm(prompt)

    async def run_setup(self) -> None:
        from phoson_cli import commands as commands_mod

        self.repl.config = await commands_mod.run_install_wizard(self.repl.config)
        await self.repl.set_model(self.repl.config.model)
        self.print_info("Setup completed.")

    def start_copy_mode(self) -> None:
        """Classic front end has no selectable chat pane (G3 is TUI-only)."""
        self.print_info(
            "Copy mode (arrow-select a range → Enter to copy) is only "
            "available in the full-screen TUI (the default front end)."
        )


__all__ = [
    "CommandHost",
    "RendererCommandHost",
    "HelpEntry",
    "HelpSection",
    "HelpEntries",
    "is_grouped_help",
]
