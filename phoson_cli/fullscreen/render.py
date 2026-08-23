"""Rich → ANSI bridge for the full-screen chat pane.

Ported from the reference prototype's ``render_chat_formatted``: every
render pass builds a fresh ``rich.console.Console`` writing into a
throwaway ``io.StringIO`` buffer, prints every renderable into it, and
returns the captured ANSI string. The caller (``PhosonApp._render_chat``)
wraps that string in ``prompt_toolkit.formatted_text.ANSI(...)`` and
caches it behind a dirty flag, since the sink's transcript is
append-only and rebuilding it from scratch on every keystroke would be
wasteful.
"""

import io

from rich.console import Console

from .sink import FullScreenSink
from ..formatting import render_streaming_panel


def render_chat(sink: FullScreenSink, width: int) -> str:
    """Render the sink's transcript (history + in-flight turn) to ANSI text."""
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        legacy_windows=False,
        highlight=False,
    )

    if not sink.blocks and sink.current_turn is None:
        console.print("Type a message and press Enter.", style=sink.theme.muted)
        return buf.getvalue()

    for block in sink.blocks:
        console.print(block)

    turn = sink.current_turn
    if turn is not None:
        console.print(
            render_streaming_panel(
                turn.content, turn.reasoning, turn.show_reasoning, sink.theme
            )
        )
        panel = sink.render_subagent_panel()
        if panel is not None:
            console.print(panel)

    return buf.getvalue()


__all__ = ["render_chat"]
