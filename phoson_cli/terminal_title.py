"""Terminal window/tab title (OSC 2) reflecting the session and run state.

Most terminals default the window title to the foreground command name
(``phoson-cli``). Phoson overrides it with the session title once one exists
(the heuristic first-message title, the background LLM title, or ``/title``),
and prefixes a ``*`` while the agent is working — e.g. ``* Refactor auth`` —
so a backgrounded window shows what is happening at a glance.

Writes go through prompt_toolkit's output when an ``Application`` is running
(its ``set_title`` is buffered and flushed with the next frame, so the title
never races the renderer); otherwise a plain OSC 2 sequence is written to a
TTY stdout. Non-TTY output (pipes, one-shot captures) is left untouched.
"""

import sys

#: Title shown when the session has no title yet.
BRAND = "phoson-cli"

_OSC = "\x1b]2;"
_BEL = "\x07"


def _sanitize(text: str) -> str:
    """Drop control characters so a title can never inject escapes."""
    return (
        text.replace("\x1b", "")
        .replace("\x07", "")
        .replace("\n", " ")
        .replace("\r", "")
    )


def format_title(session_title: str | None, working: bool) -> str:
    """Build the terminal title from the session title and run state.

    ``working`` prefixes the decorative ``*`` (``* Refactor X``); an empty or
    missing session title falls back to :data:`BRAND`.
    """
    base = " ".join((session_title or "").split()) or BRAND
    return f"* {base}" if working else base


def set_title(text: str) -> None:
    """Set the terminal title (best-effort; never raises)."""
    clean = _sanitize(text)
    try:
        from prompt_toolkit.application import get_app

        get_app().output.set_title(clean)
        return
    except Exception:  # noqa: BLE001 - no running app / unsupported output
        pass
    stream = sys.stdout
    try:
        if stream is None or not stream.isatty():
            return
        stream.write(f"{_OSC}{clean}{_BEL}")
        stream.flush()
    except Exception:  # noqa: BLE001 - a title must never break the CLI
        pass


def clear_title() -> None:
    """Restore the terminal's default title (best-effort)."""
    try:
        from prompt_toolkit.application import get_app

        get_app().output.clear_title()
        return
    except Exception:  # noqa: BLE001
        pass
    stream = sys.stdout
    try:
        if stream is None or not stream.isatty():
            return
        stream.write(f"{_OSC}{_BEL}")
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


__all__ = ["BRAND", "format_title", "set_title", "clear_title"]
