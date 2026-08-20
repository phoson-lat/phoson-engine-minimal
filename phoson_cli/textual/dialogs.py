"""Modals for the Phoson Textual TUI (phase 3)."""

from collections.abc import Iterable

from textual.events import Key
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Label, Button


class BashConfirmation(ModalScreen[bool]):
    """Yes/No modal for safe-mode bash (the ConfirmationService body).

    Dismisses with ``True`` (run) or ``False`` (decline). ``y``/``n``
    keys work in addition to the buttons; Escape declines (fail closed
    is the app-level policy — the modal simply says "no").
    """

    BINDINGS = [
        ("y", "accept", "yes"),
        ("n", "decline", "no"),
        ("escape", "decline", "cancel"),
    ]

    DEFAULT_CSS = """
    BashConfirmation {
        align: center middle;
    }
    BashConfirmation > .confirm-box {
        width: 62;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    BashConfirmation .confirm-title {
        text-style: bold;
        text-align: left;
        padding-bottom: 1;
    }
    BashConfirmation .confirm-cmd {
        text-align: left;
        text-wrap: wrap;
        background: $surface;
        padding: 0 1;
        margin-bottom: 1;
    }
    BashConfirmation .confirm-buttons {
        align: right middle;
        height: 3;
    }
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self._command = command

    def compose(self) -> Iterable[Widget]:
        with self._box():
            yield Label("Run this bash command?", classes="confirm-title")
            yield Label(self._command, classes="confirm-cmd")
            with self._buttons():
                yield Button("Yes", variant="success", id="confirm-yes")
                yield Button("No", variant="error", id="confirm-no")

    def _box(self):
        from textual.containers import Container

        return Container(classes="confirm-box")

    def _buttons(self):
        from textual.containers import Horizontal

        return Horizontal(classes="confirm-buttons")

    def on_mount(self) -> None:
        self.query_one("#confirm-yes", Button).focus()

    def on_key(self, event: "Key") -> None:
        # Explicit key handling: y accepts, n/escape decline.
        if event.key == "y":
            event.stop()
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            event.stop()
            self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)
