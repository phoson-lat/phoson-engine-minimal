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


def picker_output_capable(stream: Any) -> bool:
    """Return whether prompt_toolkit full-screen pickers can drive *stream*.

    Same as :func:`cursor_output_capable`, except on Windows: the console is
    driven through Win32 APIs (prompt_toolkit's ``Win32Output``) and ``TERM``
    is typically unset there, so requiring it would wrongly disable the
    classic REPL's dropdown pickers (``/model``, ``/provider``, ``/sessions``)
    on every Windows terminal. A TTY is sufficient on ``win32``.

    Kept separate from :func:`cursor_output_capable` on purpose: the latter
    also gates the *front-end* selection (full-screen TUI vs. classic in
    ``__main__._should_use_classic``), whose default must not change here.
    """
    if not stream_is_tty(stream):
        return False
    if sys.platform == "win32":
        return True
    return os.environ.get("TERM", "") not in {"", "dumb"}


def animation_capable(stream: Any, *, theme_name: str | None = None) -> bool:
    """Return whether animated cursor output and color are both permitted."""
    no_color = (
        bool(os.environ.get("NO_COLOR", "").strip())
        or os.environ.get("CLICOLOR") == "0"
    )
    return cursor_output_capable(stream) and not no_color and theme_name != "no-color"
