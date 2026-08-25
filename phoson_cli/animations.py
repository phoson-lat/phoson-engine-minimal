"""Shared animation frame sequences.

Single source of truth for the braille spinner frames used by every
animated UI surface (classic renderer, full-screen sink, subagent panel).
Previously the same 10-frame sequence was copy-pasted in three modules.
"""

#: Standard braille spinner, one full rotation (10 frames).
SPINNER_FRAMES: tuple[str, ...] = (
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
)


def spinner_frame(index: int) -> str:
    """The spinner frame at ``index`` (wraps around the full rotation)."""
    return SPINNER_FRAMES[index % len(SPINNER_FRAMES)]


__all__ = ["SPINNER_FRAMES", "spinner_frame"]
