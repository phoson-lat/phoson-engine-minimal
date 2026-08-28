"""Rich → ANSI bridge for the full-screen chat pane.

Ported from the reference prototype's ``render_chat_formatted``: every
render pass builds a fresh ``rich.console.Console`` writing into a
throwaway ``io.StringIO`` buffer, prints every renderable into it, and
returns the captured ANSI string. The caller (``PhosonApp._render_chat``)
wraps that string in ``prompt_toolkit.formatted_text.ANSI(...)``.

Performance (perf/render-cache): transcript blocks are immutable once
appended, so each block is rendered to its ANSI string exactly once and
cached (keyed by block identity + width). Subsequent render passes only
concatenate cached strings and re-render the live streaming panel —
turning per-frame cost from O(entire transcript) into O(new blocks).

Hyperlinks (IMPROVEMENTS.md G4, #58): ``formatting.py`` now lets Rich's
``Markdown`` emit real OSC 8 hyperlink escapes, which
``prompt_toolkit.formatted_text.ANSI()`` doesn't understand on its own and
would tear apart into literal text. ``BlockAnsiCache.get_or_render`` runs
``osc8_passthrough`` on each block's freshly rendered ANSI string before
caching it — once per block per width, same as the render itself — so the
sequence survives ``ANSI()``'s parse. Only cached transcript blocks can
carry Markdown links; the in-flight turn streams as plain text
(``stream_plain=True``) until it's frozen into a cached block, so nothing
else in this module needs the same treatment.
"""

import io

from rich.console import Console

from .sink import FullScreenSink
from ..formatting import render_activity_line, render_streaming_panel
from ..hyperlinks import osc8_passthrough


def _make_console(buf: io.StringIO, width: int) -> Console:
    return Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        legacy_windows=False,
        highlight=False,
    )


class BlockAnsiCache:
    """ANSI-string cache for immutable transcript blocks.

    Keyed by the renderable object itself (via ``id``) plus the pane
    width — blocks are append-only and never mutated, so a cached string
    stays valid until the width changes. Entries hold a strong reference
    to the block alongside its rendered text, so an id can never be
    matched against a different (recycled) object. A single ``Console``
    is reused across renders (it only carries the target buffer/width).
    """

    __slots__ = ("_entries", "_width", "_console", "_buf")

    def __init__(self) -> None:
        self._entries: dict[int, tuple[object, str]] = {}
        self._width: int = 0
        self._buf = io.StringIO()
        self._console: Console | None = None

    def get_or_render(self, block: object, width: int) -> str:
        """Return the cached ANSI string for *block*, rendering if needed."""
        if width != self._width:
            self.clear(width)
        key = id(block)
        entry = self._entries.get(key)
        if entry is not None and entry[0] is block:
            return entry[1]
        # Reuse one Console + buffer; capture its output per render.
        if self._console is None or self._console.width != width:
            self._buf.seek(0)
            self._buf.truncate()
            self._console = _make_console(self._buf, width)
        else:
            self._buf.seek(0)
            self._buf.truncate()
        assert self._console is not None
        self._console.print(block)
        text = osc8_passthrough(self._buf.getvalue())
        self._entries[key] = (block, text)
        return text

    def clear(self, width: int) -> None:
        """Drop all entries (e.g. on terminal resize)."""
        self._entries.clear()
        self._width = width


def render_chat(
    sink: FullScreenSink, width: int, cache: BlockAnsiCache | None = None
) -> str:
    """Render the sink's transcript (history + in-flight turn) to ANSI text.

    When *cache* is given (the app's steady-state path), immutable
    transcript blocks are rendered at most once per width; without it,
    behaviour matches the original uncached implementation.
    """
    own_cache = cache is None
    if own_cache:
        cache = BlockAnsiCache()
    assert cache is not None

    buf = io.StringIO()

    if not sink.blocks and sink.current_turn is None:
        _make_console(buf, width).print(
            "Type a message and press Enter.", style=sink.theme.muted
        )
        return buf.getvalue()

    for block in sink.blocks:
        buf.write(cache.get_or_render(block, width))

    turn = sink.current_turn
    if turn is not None:
        console = _make_console(buf, width)
        # The indicator is deliberately inside the chat pane rather than the
        # header. It is transient: shown from Enter until completion/cancel,
        # including the provider-startup gap before AgentStartEvent arrives.
        console.print(
            render_activity_line(
                sink.activity_text(), sink.activity_frame(), sink.theme
            )
        )
        # Do not show an empty assistant block alongside the activity line.
        # In particular, a provider can emit reasoning while the user has it
        # hidden: ``render_streaming_panel`` would then fall back to its own
        # ``Phoson / thinking...`` placeholder, duplicating the spinner above.
        # Render the assistant panel only for visible content or visible
        # reasoning; otherwise the activity line is the sole feedback.
        if turn.content or (turn.reasoning and turn.show_reasoning):
            # stream_plain=True while the turn is in flight: re-parsing
            # growing markdown every frame is the single hottest render path
            # (perf/render-cache). The frozen transcript gets real Markdown.
            console.print(
                render_streaming_panel(
                    turn.content,
                    turn.reasoning,
                    turn.show_reasoning,
                    sink.theme,
                    stream_plain=True,
                )
            )
        panel = sink.render_subagent_panel()
        if panel is not None:
            console.print(panel)

    return buf.getvalue()


__all__ = ["BlockAnsiCache", "render_chat"]
