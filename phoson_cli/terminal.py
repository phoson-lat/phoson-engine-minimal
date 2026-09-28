"""Fail-closed terminal capability probes shared by CLI frontends."""

import os
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


def animation_capable(stream: Any, *, theme_name: str | None = None) -> bool:
    """Return whether animated cursor output and color are both permitted."""
    no_color = (
        bool(os.environ.get("NO_COLOR", "").strip())
        or os.environ.get("CLICOLOR") == "0"
    )
    return cursor_output_capable(stream) and not no_color and theme_name != "no-color"
