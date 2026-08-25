"""Key bindings for the full-screen app.

Kept separate from ``app.py`` so bindings can be exercised directly in
tests (call the bound handler function) without a running
``Application`` — same pattern used by ``phoson_cli/pickers/_base.py``.
"""

from typing import TYPE_CHECKING

from prompt_toolkit.key_binding import KeyBindings

if TYPE_CHECKING:
    from .app import PhosonApp


def build_key_bindings(app: "PhosonApp") -> KeyBindings:
    """Build the global key bindings for ``app``."""
    kb = KeyBindings()

    @kb.add("c-q")
    @kb.add("c-c")
    def _exit(event: object) -> None:  # noqa: ARG001
        app.request_exit()

    @kb.add("enter")
    def _submit(event: object) -> None:  # noqa: ARG001
        app.submit()

    @kb.add("c-j")
    def _newline(event: object) -> None:  # noqa: ARG001
        app.insert_newline()

    @kb.add("pageup")
    def _page_up(event: object) -> None:  # noqa: ARG001
        app.scroll_page_up()

    @kb.add("pagedown")
    def _page_down(event: object) -> None:  # noqa: ARG001
        app.scroll_page_down()

    @kb.add("s-up")
    @kb.add("c-up")
    def _line_up(event: object) -> None:  # noqa: ARG001
        app.scroll_line_up()

    @kb.add("s-down")
    @kb.add("c-down")
    def _line_down(event: object) -> None:  # noqa: ARG001
        app.scroll_line_down()

    @kb.add("home")
    def _home(event: object) -> None:  # noqa: ARG001
        app.scroll_home()

    @kb.add("end")
    def _end(event: object) -> None:  # noqa: ARG001
        app.scroll_end()

    @kb.add("c-l")
    def _clear(event: object) -> None:  # noqa: ARG001
        app.clear()

    @kb.add("c-t")
    def _toggle_reasoning(event: object) -> None:  # noqa: ARG001
        app.toggle_reasoning()

    @kb.add("c-d")
    def _ctrl_d(event: object) -> None:  # noqa: ARG001
        app.handle_ctrl_d()

    @kb.add("c-v")
    def _paste_image(event: object) -> None:  # noqa: ARG001
        app.paste_image()

    @kb.add("escape", eager=True)
    def _escape(event: object) -> None:  # noqa: ARG001
        app.handle_escape()

    return kb


__all__ = ["build_key_bindings"]
