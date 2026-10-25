"""Exit / Ctrl+D handling for the full-screen front end (#187).

Extracted from ``app.py``: the two-step Ctrl+C interrupt, interrupting a
visible turn vs. quitting, and
Ctrl+D's delete-forward-or-quit behavior. Kept as thin delegates on
``PhosonApp`` (``request_exit`` / ``handle_ctrl_d``) so the ``keys.py``
name lookups and the test suite keep working.
"""

import time
import asyncio
from typing import Any

#: A second Ctrl+C within this window quits; the first one only cancels the
#: running turn or clears the composer (and says so in the footer).
EXIT_CONFIRM_WINDOW_SECONDS = 2.0


def exit_armed(app: Any) -> bool:
    """True while a first Ctrl+C is waiting for its confirming second press."""
    armed_at = getattr(app, "_interrupt_armed_at", 0.0)
    return bool(armed_at) and (
        time.monotonic() - armed_at <= EXIT_CONFIRM_WINDOW_SECONDS
    )


def handle_interrupt(app: Any) -> None:
    """Ctrl+C: cancel the turn or clear the draft; pressed twice, quit.

    A lone Ctrl+C never loses work: while a turn runs it cancels it (like
    Esc), otherwise it clears the composer (and leaves shell mode). Either
    way it arms a short window in which a second Ctrl+C quits through
    :func:`request_exit` (so a protected save still completes first); the
    footer shows the hint while armed.
    """
    if exit_armed(app):
        app._interrupt_armed_at = 0.0
        request_exit(app)
        return
    if app._is_run_in_flight():
        result = app._cancel_operation()
        if result == "protected":
            app.sink.notify(
                "info", "Saving session; required persistence cannot be cancelled."
            )
        elif result == "cancelled":
            app.sink.notify("info", "Cancelling current operation (Ctrl+C)...")
    elif app._prompt_input.text or app._shell_mode:
        app._prompt_input.text = ""
        app._shell_mode = False
    app._interrupt_armed_at = time.monotonic()
    app.app.invalidate()
    try:
        # Repaint once the window lapses so the footer hint goes away.
        asyncio.get_running_loop().call_later(
            EXIT_CONFIRM_WINDOW_SECONDS + 0.05, app.app.invalidate
        )
    except RuntimeError:  # no running loop (tests driving the handler)
        pass


def request_exit(app: Any) -> None:
    """Ctrl+Q (or a confirmed Ctrl+C): interrupt a visible turn, or quit.

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


__all__ = [
    "EXIT_CONFIRM_WINDOW_SECONDS",
    "exit_armed",
    "handle_interrupt",
    "request_exit",
    "handle_ctrl_d",
]
