"""Exit / Ctrl+D handling for the full-screen front end (#187).

Extracted from ``app.py``: interrupting a visible turn vs. quitting, and
Ctrl+D's delete-forward-or-quit behavior. Kept as thin delegates on
``PhosonApp`` (``request_exit`` / ``handle_ctrl_d``) so the ``keys.py``
name lookups and the test suite keep working.
"""

from typing import Any


def request_exit(app: Any) -> None:
    """Ctrl+C/Ctrl+Q: interrupt a visible turn, or quit.

    The app-managed outer task covers commands, preparation, bash, palette
    dispatch, and wake turns in addition to the controller's stream task.
    Normal work is cancelled through that outer task. Once an agent terminal
    event makes session persistence mandatory, exit is deferred until the
    same task has fully settled.
    """
    if app._is_run_in_flight():
        result = app._cancel_operation(exit_when_done=True)
        if result == "protected":
            app.sink.notify("info", "Saving session before exit...")
        return
    app.app.exit()


def handle_ctrl_d(app: Any) -> None:
    """Ctrl+D: delete-forward on a non-empty line, else quit.

    Unlike ``PromptSession`` (where an empty-buffer Ctrl+D raises
    ``EOFError`` for free), ``TextArea`` has no such behavior built
    in, so this is spelled out explicitly. Routed through
    ``request_exit`` rather than an unconditional quit so it stays
    consistent with Ctrl+C/Ctrl+Q (interrupts a visible turn first).
    """
    if app._prompt_input.text:
        app._prompt_input.buffer.delete()
    else:
        app.request_exit()


__all__ = ["request_exit", "handle_ctrl_d"]
