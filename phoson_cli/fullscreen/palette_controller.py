"""Command palette (Ctrl+P) host for the full-screen front end (#187).

Extracted from ``app.py``: the palette opens as a modal Float over every
slash command and dispatches the chosen one through the normal
``/command`` path. Kept as thin delegates on ``PhosonApp`` so the
``keys.py`` name lookups and the test suite (``app.open_command_palette``,
``app._palette_open``) keep working.
"""

from typing import Any


class PaletteController:
    def __init__(self, app: Any) -> None:
        self._app = app

    def open(self) -> None:
        """Ctrl+P: open the command palette over every slash command (T-12).

        The palette is a modal Float (like the model/theme pickers), so it
        can be opened from a calm screen and its confirm dispatches the
        chosen command through the normal ``/command`` path.
        """
        app = self._app
        if app._active_float is not None:
            return  # a picker/confirmation is already open
        if app._is_run_in_flight():
            app.sink.notify(
                "warn",
                "A turn is already running — press Esc to cancel it first.",
            )
            return
        if app._palette_open:
            return  # a palette is already scheduled/animating open
        app._palette_open = True
        app.app.create_background_task(self._run())

    async def _run(self) -> None:
        """Host the palette as a background task with a synchronous guard.

        ``_active_float`` is only set when the task actually runs (the
        float is opened inside the task), so a fast second Ctrl+P before
        the first task ticks would schedule a second palette and clobber
        ``_active_float`` / ``_float_kb``. ``app._palette_open`` closes
        that window; it is released in ``finally`` so a failure path
        (e.g. no entries, exception) can't wedge the guard.
        """
        app = self._app
        try:
            await self._run_inner()
        finally:
            app._palette_open = False

    async def _run_inner(self) -> None:
        from ..commands import Command
        from ..palette_picker import (
            PaletteEntry,
            PalettePickerResult,
            build_command_palette,
        )

        app = self._app
        catalog = app.repl._controller.command_catalog
        entries: list[PaletteEntry] = []
        for spec in catalog.specs:
            display = " · ".join(spec.names) if len(spec.names) > 1 else spec.primary
            entries.append(
                PaletteEntry(
                    name=spec.primary,
                    display=display,
                    help=spec.help,
                )
            )
        if not entries:
            app.sink.notify("info", "No commands available.")
            return
        picker = build_command_palette(entries, theme=app.theme)
        result = await app.run_float_picker(picker)
        if not isinstance(result, PalettePickerResult):
            return
        if result.cancelled or not result.command_name:
            return
        if app._is_run_in_flight():
            # A run could have started while the float was open.
            app.sink.notify(
                "warn",
                "A turn is already running — press Esc to cancel it first.",
            )
            return
        await app._run_command(Command(name=result.command_name, args=""))


__all__ = ["PaletteController"]
