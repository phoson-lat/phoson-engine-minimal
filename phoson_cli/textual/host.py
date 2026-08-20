"""Textual :class:`~phoson_cli.command_host.CommandHost` (phase 4)."""

from typing import TYPE_CHECKING, cast

from phoson_agent.sessions.models import SessionMeta

from ..models import ModelOption
from .screens import ModelPickerScreen, SessionPickerScreen, ProviderPickerScreen
from ..model_picker import ModelPickerResult
from ..session_picker import SessionPickerResult
from ..provider_picker import ProviderPickerResult

if TYPE_CHECKING:
    from .app import PhosonTextualApp


class TextualCommandHost:
    """Presents command output as conversation rows and pickers as modals."""

    def __init__(self, app: "PhosonTextualApp") -> None:
        self._app = app

    def print_info(self, message: str) -> None:
        self._app._notify("info", message)

    def print_warn(self, message: str) -> None:
        self._app._notify("warn", message)

    def print_error(self, message: str) -> None:
        self._app._notify("error", message)

    def print_help(self, entries: list[tuple[str, str]]) -> None:
        lines = [f"{name}  —  {help_text}" for name, help_text in entries]
        self._app._notify("info", "commands:\n" + "\n".join(lines))

    async def pick_model(
        self, models: list[ModelOption], current_model: str
    ) -> ModelPickerResult:
        return cast(
            ModelPickerResult,
            await self._wait_modal(ModelPickerScreen(models, current_model)),
        )

    async def pick_provider(
        self, providers: list[str], current_provider: str
    ) -> ProviderPickerResult:
        return cast(
            ProviderPickerResult,
            await self._wait_modal(ProviderPickerScreen(providers, current_provider)),
        )

    async def pick_session(
        self, sessions: list[SessionMeta], current_id: str
    ) -> SessionPickerResult:
        return cast(
            SessionPickerResult,
            await self._wait_modal(SessionPickerScreen(sessions, current_id)),
        )

    async def confirm(self, prompt: str) -> bool:
        return await self._app.ask_confirmation(prompt)

    async def run_setup(self) -> None:
        self.print_info(
            "The setup wizard is fullscreen on the classic UI. "
            "Quit the TUI and run: phoson-cli --setup"
        )

    async def _wait_modal(self, screen: object) -> object:
        """Push a modal and await its dismiss value (Textual 8.x worker-safe)."""
        import asyncio

        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()

        def _on_result(result: object) -> None:
            if not future.done():
                future.set_result(result)

        self._app.push_screen(screen, callback=_on_result)  # type: ignore[arg-type]
        value = await future
        self._app.focus_composer()
        return value


class TextualSessionFacade:
    """``CommandHandler``-shaped view of the TUI's :class:`SessionController`.

    The handler was written against ``PhosonRepl``. This facade exposes the
    same attributes/methods, routing mutations through the controller and
    refreshing the TUI (clear on ``/new``, replay on ``/sessions``).
    """

    def __init__(self, app: "PhosonTextualApp") -> None:
        self._app = app

    @property
    def _controller(self):
        controller = self._app._controller
        assert controller is not None
        return controller

    @property
    def config(self):
        return self._controller.config

    @config.setter
    def config(self, value) -> None:
        self._controller.config = value

    @property
    def current_model(self) -> str:
        return self._controller.current_model

    @property
    def subagent_model(self) -> str:
        return self._controller.subagent_model

    @subagent_model.setter
    def subagent_model(self, value: str) -> None:
        self._controller.subagent_model = value

    @property
    def engine(self):
        return self._controller.engine

    @property
    def attachments(self):
        return self._controller.attachments

    @property
    def storage(self):
        return self._controller.storage

    @property
    def tree(self):
        return self._controller.tree

    @property
    def session_metrics(self):
        return self._controller.session_metrics

    @property
    def theme(self):
        from ..theme import load_theme

        return load_theme(getattr(self._controller.config, "theme", None))

    def set_model(self, model: str) -> None:
        self._controller.set_model(model)
        self._app.update_status_bar()

    def set_provider(self, provider: str) -> None:
        self._controller.set_provider(provider)
        self._app.update_status_bar()

    async def new_session(self) -> None:
        self._controller.new_session()
        await self._app.reset_conversation()
        self._app.update_status_bar()

    async def load_session(self, session_id: str) -> bool:
        # Clear first so print_history (called by the controller) is not
        # wiped by a later empty-out — the previous TUI path did that.
        await self._app.reset_conversation()
        outcome = await self._controller.load_session(session_id)
        self._app.update_status_bar()
        return outcome.ok

    def label_current_node(self, text: str) -> None:
        self._controller.label_current_node(text)

    def undo_last_turn(self) -> tuple[bool, str]:
        return self._controller.undo_last_turn()

    def render_tree_ascii(self) -> str:
        from .._views import render_tree_ascii

        return render_tree_ascii(
            self._controller.tree, self._controller.current_node_id
        )
