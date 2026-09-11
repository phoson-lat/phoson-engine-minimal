"""Exit / Ctrl+D handling for the full-screen front end (#187).

Extracted from ``app.py``: interrupting a visible turn vs. quitting, and
Ctrl+D's delete-forward-or-quit behavior. Kept as thin delegates on
``PhosonApp`` (``request_exit`` / ``handle_ctrl_d``) so the ``keys.py``
name lookups and the test suite keep working.
"""

from typing import Any


def request_exit(app: Any) -> None:
    """Ctrl+C/Ctrl+Q: interrupt a visible turn, or quit.

    ``sink.current_turn`` is set exactly while there is something
    the user can see happening (tokens, a running tool, a tool
    awaiting confirmation) — and, because ``AgentDoneEvent``/
    ``AgentErrorEvent`` are dispatched to the sink from inside the
    same stream-consumption task ``is_running`` reflects, the two
    become False together. There is no window where content is
    still visibly streaming but ``is_running`` has already gone
    False, so ``cancel_current()`` is always effective here.

    Once the turn's content is fully rendered, only invisible
    trailing bookkeeping remains (persisting reasoning, saving the
    session) — not cancel-worthy, so this just quits; a pending
    background task gets cancelled for free by the Application
    shutting down.
    """
    if app.sink.current_turn is not None:
        app.repl.cancel_current()
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
