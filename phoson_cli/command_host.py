"""Presentation port for :class:`~phoson_cli.commands.CommandHandler`.

The handler owns command *semantics* (what ``/model`` does). A host owns
how those effects are shown and how interactive choices are collected:

- Classic REPL: :class:`RendererCommandHost` (Rich + prompt_toolkit pickers).
- Textual TUI: a widget/modal host (see ``phoson_cli.textual.host``).

Unit tests keep constructing ``CommandHandler(dummy_repl)`` — the
renderer-backed host is the default when none is injected.
"""

from typing import Any, Protocol, runtime_checkable

from phoson_agent.sessions.models import SessionMeta

from .models import ModelOption
from .model_picker import ModelPickerResult
from .session_picker import SessionPickerResult
from .provider_picker import ProviderPickerResult


@runtime_checkable
class CommandHost(Protocol):
    """UI adapter used by :class:`~phoson_cli.commands.CommandHandler`."""

    def print_info(self, message: str) -> None: ...

    def print_warn(self, message: str) -> None: ...

    def print_error(self, message: str) -> None: ...

    def print_help(self, entries: list[tuple[str, str]]) -> None: ...

    async def pick_model(
        self, models: list[ModelOption], current_model: str
    ) -> ModelPickerResult: ...

    async def pick_provider(
        self, providers: list[str], current_provider: str
    ) -> ProviderPickerResult: ...

    async def pick_session(
        self, sessions: list[SessionMeta], current_id: str
    ) -> SessionPickerResult: ...

    async def confirm(self, prompt: str) -> bool: ...

    async def run_setup(self) -> None: ...


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

    def print_help(self, entries: list[tuple[str, str]]) -> None:
        renderer = self.repl.renderer
        printer = getattr(renderer, "print_help", None)
        if printer is not None:
            printer(entries)
            return
        for name, help_text in entries:
            renderer.print_info(f"{name}  {help_text}")

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
        self.repl.set_model(self.repl.config.model)
        self.print_info("Setup completed.")


__all__ = ["CommandHost", "RendererCommandHost"]
