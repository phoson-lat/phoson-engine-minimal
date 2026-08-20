"""Interactive pickers for the Phoson Textual TUI (phase 4)."""

from typing import TypeVar
from collections.abc import Iterable

from textual.screen import ModalScreen
from textual.widget import Widget
from textual.binding import Binding
from textual.widgets import Input, Label, OptionList
from textual.containers import Vertical
from textual.widgets.option_list import Option

from phoson_agent.sessions.models import SessionMeta

from ..models import ModelOption
from ..model_picker import (
    ModelPickerResult,
    _format_meta,
    _filter_models,
    _format_context_length,
)
from ..session_picker import SessionPickerResult
from ..provider_picker import _PROVIDER_LABELS, ProviderPickerResult

T = TypeVar("T")


_PICKER_CSS = """
ModelPickerScreen, ProviderPickerScreen, SessionPickerScreen {
    align: center middle;
}
.picker-box {
    width: 90;
    max-width: 96%;
    height: auto;
    max-height: 80%;
    padding: 1 1;
    border: round $accent;
    background: $surface;
}
.picker-title {
    text-style: bold;
    padding: 0 1 1 1;
}
#picker-search {
    margin: 0 1 1 1;
}
#picker-list {
    height: 16;
    margin: 0 1;
}
.picker-hint {
    color: $text-muted;
    padding: 1 1 0 1;
}
"""


class _PickerScreen(ModalScreen[T]):
    """Shared chrome for the TUI pickers."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
    ]
    DEFAULT_CSS = _PICKER_CSS

    def action_cancel(self) -> None:
        raise NotImplementedError


class ModelPickerScreen(_PickerScreen[ModelPickerResult]):
    """Fuzzy-search model list (``/model`` with no argument)."""

    DEFAULT_CSS = _PICKER_CSS

    def __init__(self, models: list[ModelOption], current_model: str) -> None:
        super().__init__()
        self._all = list(models)
        self._current = current_model
        self._visible: list[ModelOption] = list(models)

    def compose(self) -> Iterable[Widget]:
        with Vertical(classes="picker-box"):
            yield Label("Select a model", classes="picker-title")
            yield Input(placeholder="type to filter…", id="picker-search")
            yield OptionList(id="picker-list")
            yield Label(
                "↑/↓ navigate  ·  Enter select  ·  Esc cancel",
                classes="picker-hint",
            )

    def on_mount(self) -> None:
        self._rebuild("")
        self.query_one("#picker-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "picker-search":
            self._rebuild(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        model_id = event.option.id
        self.dismiss(ModelPickerResult(model_id=model_id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "picker-search":
            self._select_highlighted()

    def on_key(self, event) -> None:
        listing = self.query_one("#picker-list", OptionList)
        if event.key == "down":
            event.stop()
            listing.action_cursor_down()
        elif event.key == "up":
            event.stop()
            listing.action_cursor_up()
        elif event.key == "enter" and self.focused is not listing:
            event.stop()
            self._select_highlighted()

    def _select_highlighted(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        index = listing.highlighted
        if index is None or not (0 <= index < len(self._visible)):
            return
        self.dismiss(ModelPickerResult(model_id=self._visible[index].id))

    def action_cancel(self) -> None:
        self.dismiss(ModelPickerResult(cancelled=True))

    def _rebuild(self, query: str) -> None:
        self._visible = _filter_models(self._all, query)
        listing = self.query_one("#picker-list", OptionList)
        listing.clear_options()
        listing.add_options(self._options())
        if self._visible:
            listing.highlighted = 0

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for model in self._visible:
            marker = "▶" if model.id == self._current else " "
            ctx = _format_context_length(model.context_length)
            meta = _format_meta(model)
            prompt = f"{marker} {model.id}  {ctx}  {meta}"
            options.append(Option(prompt, id=model.id))
        if not options:
            options.append(Option("(no matches)", disabled=True))
        return options


class ProviderPickerScreen(_PickerScreen[ProviderPickerResult]):
    """Configured-provider list (``/provider`` with no argument)."""

    DEFAULT_CSS = _PICKER_CSS

    def __init__(self, providers: list[str], current_provider: str) -> None:
        super().__init__()
        self._providers = list(providers)
        self._current = current_provider

    def compose(self) -> Iterable[Widget]:
        with Vertical(classes="picker-box"):
            yield Label("Select a provider", classes="picker-title")
            yield OptionList(*self._options(), id="picker-list")
            yield Label(
                "↑/↓ navigate  ·  Enter select  ·  Esc cancel",
                classes="picker-hint",
            )

    def on_mount(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        listing.focus()
        if self._providers:
            current = next(
                (i for i, p in enumerate(self._providers) if p == self._current),
                0,
            )
            listing.highlighted = current

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(ProviderPickerResult(provider=event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(ProviderPickerResult(cancelled=True))

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for provider in self._providers:
            marker = "▶" if provider == self._current else " "
            label = _PROVIDER_LABELS.get(provider, provider)
            options.append(Option(f"{marker} {provider}  {label}", id=provider))
        return options


class SessionPickerScreen(_PickerScreen[SessionPickerResult]):
    """Saved-session list (``/sessions``). ``d`` deletes the highlighted row."""

    DEFAULT_CSS = _PICKER_CSS

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("d", "delete_selected", "delete", show=False),
    ]

    def __init__(self, sessions: list[SessionMeta], current_id: str) -> None:
        super().__init__()
        self._sessions = list(sessions)
        self._current = current_id

    def compose(self) -> Iterable[Widget]:
        with Vertical(classes="picker-box"):
            yield Label("Saved sessions", classes="picker-title")
            yield OptionList(*self._options(), id="picker-list")
            yield Label(
                "↑/↓ navigate  ·  Enter load  ·  d delete  ·  Esc cancel",
                classes="picker-hint",
            )

    def on_mount(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        listing.focus()
        if self._sessions:
            listing.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(SessionPickerResult(session_id=event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(SessionPickerResult(cancelled=True))

    def action_delete_selected(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        index = listing.highlighted
        if index is None or not (0 <= index < len(self._sessions)):
            return
        session_id = str(self._sessions[index].id)
        self.dismiss(SessionPickerResult(session_id=session_id, delete=True))

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for meta in self._sessions:
            sid = str(meta.id)
            marker = "▶" if sid == self._current else " "
            updated = meta.updated_at.strftime("%Y-%m-%d %H:%M")
            cost = f"${meta.total_cost:,.4f}"
            prompt = (
                f"{marker} {sid[:8]}  {meta.message_count:>3} msgs  "
                f"{updated}  {cost}  {meta.last_model or ''}"
            )
            options.append(Option(prompt, id=sid))
        return options
