"""Pure renderable formatters (UI-toolkit agnostic).

Textual migration (phase 2): formatters that turn *data* into Rich
renderables without any console/spinner/Live state live here so both
front ends can reuse them — the classic Renderer prints them to the
terminal; the Textual TUI will render the same objects inside widgets
(Textual displays Rich renderables natively).

Keep this module dependency-free of console I/O: no ``Console``, no
``print``, no ``Live``, no threads.
"""

from rich import box
from rich.text import Text
from rich.panel import Panel

from .theme import Theme


def render_reasoning_panel(reasoning: str, theme: Theme) -> Panel:
    """Build the expanded reasoning panel (Ctrl+T post-turn)."""
    return Panel(
        Text(reasoning, style=theme.reasoning),
        title="reasoning",
        title_align="left",
        border_style=theme.muted_deep,
        box=box.ROUNDED,
        padding=(0, 1),
    )


__all__ = ["render_reasoning_panel"]
