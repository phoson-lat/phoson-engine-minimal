"""Fail-closed terminal capability probes shared by CLI frontends."""

import os
import sys
from typing import Any


def stream_is_tty(stream: Any) -> bool:
    """Return whether *stream* is a TTY; malformed streams are non-capable."""
    try:
        isatty = getattr(stream, "isatty", None)
        return bool(isatty()) if callable(isatty) else False
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def cursor_output_capable(stream: Any) -> bool:
    """Return whether cursor/alternate-screen output is safe on *stream*."""
    return stream_is_tty(stream) and os.environ.get("TERM", "") not in {"", "dumb"}


def _ansi_capable(stream: Any) -> bool:
    """Whether inline ANSI cursor control (CR / clear-line) is safe on *stream*.

    On Windows a TTY is sufficient: the console is driven through Win32
    APIs / VT sequences (prompt_toolkit enables VT) and ``TERM`` is
    typically unset there — requiring it disabled the spinner and the
    subagent animation on every Windows terminal. ``TERM=dumb`` is still
    honored as an explicit opt-out. On POSIX, ``TERM`` is authoritative.

    Kept separate from :func:`cursor_output_capable`, which also gates the
    *front-end* selection (full-screen TUI vs. classic in
    ``__main__._should_use_classic``) and must not change here.
    """
    if not stream_is_tty(stream):
        return False
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return False
    if sys.platform == "win32":
        return True
    return term != ""


def animation_capable(stream: Any, *, theme_name: str | None = None) -> bool:
    """Return whether animated cursor output and color are both permitted."""
    no_color = (
        bool(os.environ.get("NO_COLOR", "").strip())
        or os.environ.get("CLICOLOR") == "0"
    )
    return _ansi_capable(stream) and not no_color and theme_name != "no-color"
