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


def _line_boundaries(s: str) -> list[int]:
    """Char offsets where each visual line starts, for *s*.

    ``bounds[k]`` is the character index of the first character of visual
    line ``k`` (line 0 starts at 0). ``len(bounds) == s.count('\\n') + 1``
    — one past the last line's start, i.e. the number of visual lines
    prompt_toolkit's ``split_lines`` would produce over *s* (it splits on
    ``\\n`` exactly like ``str.split``). A trailing newline therefore adds a
    final boundary pointing at the end of the string (the empty trailing
    line), matching ``split_lines`` which always yields a final line.

    T-14 (#171): the chat pane is *windowed* — the full cached transcript
    string is sliced at these boundaries so prompt_toolkit only ever sees
    the visible window. Because Rich re-asserts each line's SGR state after
    every newline, slicing at a line boundary and re-parsing yields the same
    *visible text* per line as parsing the full string (see
    ``test_t14_windowing_unit``); fragment boundaries at a slice edge can
    differ cosmetically (a leading empty fragment) but render identically.
    """
    bounds: list[int] = [0]
    i = 0
    while True:
        j = s.find("\n", i)
        if j < 0:
            break
        bounds.append(j + 1)
        i = j + 1
    return bounds


def windowed_slice(full_ansi: str, bounds: list[int], top: int, height: int) -> str:
    """Slice *full_ansi* to visual lines ``[top, top+height)``.

    *bounds* is ``_line_boundaries(full_ansi)``; ``bounds[k]`` is the start
    offset of visual line ``k`` and ``len(bounds)`` is the number of visual
    lines. Returns the substring from the start of line *top* to the start of
    line ``top+height`` — or the end of the string when the window reaches the
    last line (whose start is ``bounds[-1]`` and whose end is the string end,
    not another boundary). The result is a contiguous run of complete visual
    lines and re-parses to the same visible text as the full string's lines
    ``top .. top+height-1``, so ``ANSI(...)`` on it is render-equivalent.
    """
    total = len(bounds)  # number of visual lines (each bounds[k] = line k's start)
    if total <= 0 or height <= 0:
        return ""
    top = max(0, top)
    if top >= total:
        return ""
    end_line = min(top + height, total)
    start = bounds[top]
    # The window's last line is the string's last line: extend to the end.
    stop = bounds[end_line] if end_line < total else len(full_ansi)
    return full_ansi[start:stop]


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
        # T-1: real empty state — a one-line hint, no ASCII-art mascot
        # (the banner is no longer injected into the sink). Both features
        # work today: @file mentions and / commands.
        _make_console(buf, width).print(
            "  @ files  ·  / commands", style=sink.theme.muted_deep
        )
        return buf.getvalue()

    for block in sink.blocks:
        buf.write(cache.get_or_render(block, width))

    turn = sink.current_turn
    if turn is not None:
        console = _make_console(buf, width)
        show_panel = bool(turn.content) or (
            bool(turn.reasoning) and turn.show_reasoning
        )
        # The indicator is deliberately inside the chat pane rather than the
        # header. It is transient: shown from Enter until completion/cancel,
        # including the provider-startup gap before AgentStartEvent arrives.
        #
        # While a tool call is being *composed* after some text has already
        # streamed, the indicator follows the text instead of leading it
        # (I-128): the "✍ writing file…" line reads as a continuation of the
        # agent's message, not a banner stuck above it. Models usually end
        # the preceding paragraph with a trailing newline; with the
        # indicator below, that newline would show up as a blank line gap,
        # so the trailing newline(s) are dropped from the in-flight render
        # only (the frozen transcript keeps the original text).
        composing_after_panel = bool(turn.composing_tool) and show_panel
        if composing_after_panel:
            panel_content = turn.content
            if panel_content.endswith("\n"):
                panel_content = panel_content.rstrip("\n") or " "
        else:
            panel_content = turn.content
        activity = render_activity_line(
            sink.activity_text(), sink.activity_frame(), sink.theme
        )
        if not composing_after_panel:
            console.print(activity)
        # Do not show an empty assistant block alongside the activity line.
        # In particular, a provider can emit reasoning while the user has it
        # hidden: ``render_streaming_panel`` would then fall back to its own
        # ``Phoson / thinking...`` placeholder, duplicating the spinner above.
        # Render the assistant panel only for visible content or visible
        # reasoning; otherwise the activity line is the sole feedback.
        if show_panel:
            # stream_plain=True while the turn is in flight: re-parsing
            # growing markdown every frame is the single hottest render path
            # (perf/render-cache). The frozen transcript gets real Markdown.
            console.print(
                render_streaming_panel(
                    panel_content,
                    turn.reasoning,
                    turn.show_reasoning,
                    sink.theme,
                    stream_plain=True,
                )
            )
        if composing_after_panel:
            console.print(activity)
        panel = sink.render_subagent_panel()
        if panel is not None:
            console.print(panel)

    return buf.getvalue()


__all__ = ["BlockAnsiCache", "render_chat"]
