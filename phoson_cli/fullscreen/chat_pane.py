"""The windowed chat pane: scroll, windowed render, bounds cache, scrollbar.

Issue #187 (F-45), slice 2.  Extracted from ``app.py`` to give the chat pane
its own module *and* to make the T-14 windowing bugs (F-40 / F-41) unit-testable
in isolation, without instantiating :class:`PhosonApp`.

``ChatPane`` owns the pane state (scroll position, the windowed-ANSI caches,
the per-line bounds, the per-width block caches).  ``PhosonApp`` keeps thin
proxy properties / delegates so the public surface (``keys.py`` scroll
bindings, ``apply_theme`` / ``_reset_transcript`` cache resets, and the test
suite, which reads ``app._full_ansi_text`` / ``app._window_top`` / … directly)
is unchanged.

The ptk ``Window`` object (``PhosonApp._chat_window``) is created by
``_build_layout`` and is *not* owned here — the pane reads it lazily through
``self.app`` because it only matters once a live layout exists.
"""

import os
import time
import shutil
import asyncio
import logging
from typing import Any
from collections.abc import Callable

from prompt_toolkit import Application
from prompt_toolkit.mouse_events import MouseEvent, MouseButton, MouseEventType
from prompt_toolkit.formatted_text import (
    ANSI,
    FormattedText,
    AnyFormattedText,
    to_formatted_text,
)
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.containers import Window

from .render import (
    BlockAnsiCache,
    BlockFormattedTextCache,
    _line_boundaries,
    render_chat_parts,
    windowed_slice_parts,
)
from .selection import (
    Selection,
    cell_width,
    plain_line,
    word_cells,
    selected_text,
    highlight_window,
)

#: A second press on the same cell within this window is a double click
#: (select the word); a third one selects the line.
_MULTI_CLICK_SECONDS = 0.4
#: A screen column past any line's end ("select to the end of the line").
_END_OF_LINE = 1 << 20
#: Scroll step interval while a drag rests past the pane's top/bottom edge.
_EDGE_SCROLL_SECONDS = 0.05
#: A new width is laid out once it has been stable this long (a window-edge
#: drag changes it once per column), for transcripts at least this long.
_RESIZE_DEBOUNCE_SECONDS = 0.1
_RESIZE_DEBOUNCE_MIN_LINES = 400

# ── Perf logging (T-14 / I-84) ───────────────────────────────────────────────

_PERF_LOGGER = logging.getLogger("phoson.cli.perf")

# T-14 (#171): resolved once at import. The steady state of ``render_chat``
# must add no env lookup per frame, so we never re-read ``os.environ`` there.
_PERF_LOGGING = bool(os.environ.get("PHOSON_PERF"))


def perf_logger_ready() -> None:
    """Attach a stderr handler to the perf logger, once.

    The dedicated logger gets its own handler: while the TUI is up the root
    logger has a NullHandler (so raw library warnings never leak over the UI)
    and would otherwise swallow perf lines. Idempotent — safe to call from
    both the per-turn counter and the per-frame chat-pane logger.
    """
    if not _PERF_LOGGER.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s"))
        _PERF_LOGGER.addHandler(_handler)
    _PERF_LOGGER.setLevel(logging.INFO)


def _chat_perf_log(
    full_chars: int,
    total_lines: int,
    top: int,
    height: int,
    slice_ms: float,
    render_ms: float = 0.0,
) -> None:
    """T-14 (#171): per-frame chat-pane cost, verifiable on a real session.

    Called only when ``PHOSON_PERF`` is set (see :data:`_PERF_LOGGING`).

    * ``slice_ms`` — the wall time of the ``windowed_slice`` that produced the
      visible fragment, i.e. the step that used to be the O(transcript)
      ``split_lines`` / ``tuple()+hash`` pass inside prompt_toolkit. Its
      flatness across session lengths is the acceptance criterion for the
      windowing work.
    * ``render_ms`` — the wall time of the dirty-frame re-render
      (``render_chat_split`` + the incremental bounds build) when the
      transcript is dirty (a streamed token, a new block, a resize). 0.0 on
      frames that only re-sliced (scroll). While a turn streams it is
      O(in-flight tail): the joined frozen prefix is cached
      (``BlockAnsiCache.render_prefix``), its line offsets are cached
      (:meth:`ChatPane._frozen_bounds_for`), and the prefix and tail are kept
      as separate strings (never concatenated per frame). A frame that adds
      blocks pays only for the new blocks plus one prefix copy.

    The logger's handler is attached at import time (below), so the
    steady-state call is a single ``.info``.
    """
    if not _PERF_LOGGING:
        return
    _PERF_LOGGER.info(
        "perf chat-pane: full_chars=%d total_lines=%d top=%d height=%d "
        "render_ms=%.3f slice_ms=%.3f",
        full_chars,
        total_lines,
        top,
        height,
        render_ms,
        slice_ms,
    )


# Attach the handler once, at import, when perf logging is on. While the TUI
# is up the root logger carries a NullHandler (so raw library warnings never
# leak over the UI); without a dedicated handler these lines would be lost.
if _PERF_LOGGING:
    perf_logger_ready()


def enable_perf_counter(app: Application) -> Callable[[], int] | None:
    """Attach the per-turn render counter (I-84, phase 0).

    Enabled by ``PHOSON_PERF=1``: logs one line per agent turn with the
    number of full render passes prompt_toolkit performed during the turn
    and the effective fps. The counter reads ``Application.render_counter``
    (already maintained by prompt_toolkit), so the steady-state cost with the
    env var unset is a single ``bool(None)`` check in ``_run_turn``.

    The dedicated logger gets its own stderr handler (see
    :func:`perf_logger_ready`) so these lines survive the TUI's root-logger
    NullHandler.
    """
    if app.render_counter is None:  # defensive: never crashes if renamed
        return None
    perf_logger_ready()

    def _count() -> int:
        return app.render_counter or 0

    return _count


# ── Scrollbar ────────────────────────────────────────────────────────────────


class ChatScrollbarMargin(Margin):
    """1-column scrollbar for the windowed chat pane (T-14 / #171).

    ptk's built-in ``ScrollbarMargin`` derives its thumb from
    ``window_render_info.content_height`` / ``displayed_lines`` — which,
    once the chat pane is windowed (content == visible window only, see
    :meth:`ChatPane.render_chat`), both equal the visible height and the
    thumb would fill the whole bar. This margin instead takes the
    *transcript* totals from a callback bound to the app's scroll state
    (``total_lines`` and ``scroll_top``), and computes the thumb from
    ``visible_height = min(height, total_lines)`` — the same formula the
    built-in uses, but against the real transcript rather than the
    windowed content.
    """

    __slots__ = ("_callback",)

    def __init__(self, callback: Callable[[], tuple[int, int]]) -> None:
        """*callback* returns ``(total_lines, scroll_top)`` of the full
        transcript. Called once per frame (during margin render only)."""
        self._callback = callback

    def get_width(self, get_ui_content: Callable[[], Any]) -> int:
        return 1

    def create_margin(
        self, window_render_info: Any, width: int, height: int
    ) -> list[tuple[str, str]]:
        total, scroll = self._callback()
        # Nothing to scroll (empty chat, short transcript): no bar at all,
        # rather than a full-height thumb that reads as a stray grey column.
        if total <= height or height <= 0:
            return []
        # Mirror ScrollbarMargin's thumb math, but against the *transcript*
        # (total/scroll from the callback) rather than the windowed content
        # (which always equals the visible height). fraction_visible and
        # fraction_above use the same expressions the built-in does.
        fraction_visible = min(height, total) / float(total)
        fraction_above = min(max(0, scroll), max(0, total - height)) / float(total)
        scrollbar_height = int(min(height, max(1, height * fraction_visible)))
        scrollbar_top = int(height * fraction_above)

        def is_scroll_button(row: int) -> bool:
            return scrollbar_top <= row <= scrollbar_top + scrollbar_height

        scrollbar_background = "class:scrollbar.background"
        scrollbar_background_start = "class:scrollbar.background,scrollbar.start"
        scrollbar_button = "class:scrollbar.button"
        scrollbar_button_end = "class:scrollbar.button,scrollbar.end"

        result: list[tuple[str, str]] = []
        for i in range(height):
            if is_scroll_button(i):
                if not is_scroll_button(i + 1):
                    result.append((scrollbar_button_end, " "))
                else:
                    result.append((scrollbar_button, " "))
            else:
                if is_scroll_button(i + 1):
                    result.append((scrollbar_background_start, " "))
                else:
                    result.append((scrollbar_background, " "))
            result.append(("", "\n"))
        return result


# ── Chat pane ────────────────────────────────────────────────────────────────


class ChatPane:
    """Scroll + windowed render + per-line bounds for the chat transcript.

    Owns the pane state (see ``__init__``) and the rendering/windowing logic.
    Reads ``sink`` and the ptk ``chat_window`` lazily from the owning
    ``PhosonApp`` (``self.app``); the ``sink`` exists before this pane is
    created, but the ``chat_window`` is built in ``_build_layout`` and is only
    consulted during a live render, so a lazy read is correct.

    Constructing this class directly (with a stub ``sink`` and no app) is what
    the F-40/F-41 unit tests use to exercise :meth:`compute_chat_bounds`
    without instantiating :class:`PhosonApp`.
    """

    def __init__(self, app: Any = None, sink: Any = None) -> None:
        self.app = app
        # ``sink`` override lets the F-40/F-41 unit tests construct a pane
        # with a stub sink and *no* owning app (the issue's acceptance
        # criterion: ChatPane is testable without instantiating PhosonApp).
        self._sink_override = sink
        # Scroll + windowing state (moved from PhosonApp.__init__ in #187).
        self._chat_scroll_top = 0
        self._auto_scroll = True
        self._total_chat_lines = 1
        self._cache_dirty = True
        self._last_width = 80
        # T-14 (#171) — windowed chat render. The transcript is cached as
        # two ANSI strings — the frozen prefix (immutable blocks) and the
        # in-flight tail — each with its own line bounds (re-rendered only
        # when dirty/resized); prompt_toolkit is handed only the *visible
        # window* substring (``windowed_slice_parts``). The full string /
        # full bounds are materialised lazily, only when something reads
        # ``_full_ansi_text`` / ``_full_ansi_bounds`` (tests, legacy callers).
        self._win_prefix = ""
        self._win_prefix_bounds: list[int] = [0]
        self._win_tail = ""
        self._win_tail_bounds: list[int] = [0]
        self._full_text_cache: str | None = ""
        self._full_bounds_cache: list[int] | None = self._win_prefix_bounds
        # T-14 follow-up: incremental line-bounds for the frozen prefix.
        # ``_frozen_prefix_text`` is the exact string ``_frozen_ansi_bounds``
        # describes (the invariant the append fast path relies on).
        self._frozen_ansi_bounds: list[int] = [0]
        self._frozen_ansi_ids: tuple[object, ...] | None = None
        self._frozen_prefix_text: str | None = None
        # Bumped on every dirty re-render; the slice cache refreshes on it.
        self._chat_content_epoch = 0
        self._window_top = -1
        self._window_total = -1
        self._window_height = -1
        self._window_epoch = -1
        self._windowed_ansi = ANSI("")
        # Immutable transcript blocks render to ANSI/FormattedText once per width.
        self._block_ansi_cache = BlockAnsiCache()
        self._block_ft_cache = BlockFormattedTextCache()
        # Drag-to-select state (see the "Drag-to-select" section below).
        self._selection: Selection | None = None
        self._sel_anchor: tuple[int, int] | None = None  # set while a button is down
        self._sel_dragging = False
        self._sel_click: tuple[float, tuple[int, int] | None, int] = (0.0, None, 0)
        self._sel_fingerprint: tuple[object, ...] = ()
        self._sel_epoch = -1
        self._sel_edge = 0  # -1 / +1 while the pointer is past the top / bottom
        self._sel_x = self._sel_y = 0
        self._sel_edge_task: asyncio.Task | None = None
        self._sel_overlay: AnyFormattedText = ""
        self._sel_overlay_source: ANSI | None = None
        self._sel_overlay_key: tuple[object, ...] | None = None
        # Resize debounce: the width a re-layout is waiting for, and since when.
        self._resize_width = 0
        self._resize_since = 0.0
        self._resize_timer: asyncio.TimerHandle | None = None

    # ── Lazy views onto the owning app ─────────────────────────────────────

    @property
    def sink(self) -> Any:
        if self._sink_override is not None:
            return self._sink_override
        return self.app.sink

    # ── Full-transcript views (lazy; off the per-frame hot path) ───────────

    @property
    def _full_ansi_text(self) -> str:
        """The whole transcript ANSI (``prefix + tail``), built on demand.

        Cached until the next dirty frame, so repeated reads (and the
        "not re-rendered on scroll" identity checks) see one stable object.
        """
        if self._full_text_cache is None:
            self._full_text_cache = self._win_prefix + self._win_tail
        return self._full_text_cache

    @_full_ansi_text.setter
    def _full_ansi_text(self, text: str) -> None:
        # Seeding seam (tests): the given text becomes the whole window
        # source, with no separate tail. Set ``_full_ansi_bounds`` alongside.
        self._win_prefix = text
        self._win_tail = ""
        self._win_tail_bounds = [0]
        self._full_text_cache = text
        self._full_bounds_cache = None

    @property
    def _full_ansi_bounds(self) -> list[int]:
        """Line starts of :attr:`_full_ansi_text` (spliced on demand)."""
        if self._full_bounds_cache is None:
            tail_bounds = self._win_tail_bounds
            if len(tail_bounds) == 1:
                self._full_bounds_cache = self._win_prefix_bounds
            else:
                plen = len(self._win_prefix)
                self._full_bounds_cache = self._win_prefix_bounds + [
                    plen + b for b in tail_bounds[1:]
                ]
        return self._full_bounds_cache

    @_full_ansi_bounds.setter
    def _full_ansi_bounds(self, bounds: list[int]) -> None:
        self._win_prefix_bounds = bounds
        self._win_tail_bounds = [0]
        self._full_bounds_cache = bounds

    @property
    def chat_window(self) -> Window:
        return self.app._chat_window

    def invalidate(self) -> None:
        self.app.app.invalidate()

    # ── Scroll ─────────────────────────────────────────────────────────────

    def get_visible_window_height(self) -> int:
        render_info = self.chat_window.render_info
        if render_info is not None:
            return max(1, render_info.window_height)
        term_lines = shutil.get_terminal_size((80, 24)).lines
        return max(1, term_lines - 5)

    def get_effective_scroll(self, window: Window | None = None) -> int:
        visible_height = self.get_visible_window_height()
        max_scroll = max(0, self._total_chat_lines - visible_height)
        if self._auto_scroll:
            sink = self.sink
            if (
                getattr(sink, "welcome_provider", None) is not None
                and not sink.blocks
                and sink.current_turn is None
            ):
                # The welcome screen reads top-down (banner first): on a short
                # pane keep its top in view instead of following the bottom.
                return 0
            return max_scroll
        return max(0, min(self._chat_scroll_top, max_scroll))

    def get_chat_cursor_position(self) -> Point:
        # T-14 (#171): the control only holds the visible slice, so the
        # (hidden) cursor sits at the top of that slice. ptk's
        # ``_scroll_without_linewrapping`` clamps ``vertical_scroll`` to keep
        # the cursor visible — with the cursor at y=0 the window's own scroll
        # stays at 0 and the logical transcript scroll is entirely ours
        # (via ``_chat_scroll_top`` / the windowing in ``render_chat``).
        return Point(x=0, y=0)

    def scroll_page_up(self) -> None:
        current = self.get_effective_scroll()
        self._auto_scroll = False
        step = max(5, self.get_visible_window_height() // 2)
        self._chat_scroll_top = max(0, current - step)
        self.invalidate()

    def scroll_page_down(self) -> None:
        current = self.get_effective_scroll()
        step = max(5, self.get_visible_window_height() // 2)
        max_scroll = max(0, self._total_chat_lines - self.get_visible_window_height())
        if current + step >= max_scroll:
            self._auto_scroll = True
            self._chat_scroll_top = max_scroll
        else:
            self._chat_scroll_top = current + step
        self.invalidate()

    def scroll_line_up(self) -> None:
        current = self.get_effective_scroll()
        self._auto_scroll = False
        self._chat_scroll_top = max(0, current - 2)
        self.invalidate()

    def scroll_line_down(self) -> None:
        current = self.get_effective_scroll()
        max_scroll = max(0, self._total_chat_lines - self.get_visible_window_height())
        if current + 2 >= max_scroll:
            self._auto_scroll = True
            self._chat_scroll_top = max_scroll
        else:
            self._chat_scroll_top = current + 2
        self.invalidate()

    def scroll_home(self) -> None:
        self._auto_scroll = False
        self._chat_scroll_top = 0
        self.invalidate()

    def scroll_end(self) -> None:
        self._auto_scroll = True
        self.invalidate()

    def _scroll_by(self, delta: int) -> None:
        """Scroll *delta* lines (negative = up), following again at the end."""
        current = self.get_effective_scroll()
        max_scroll = max(0, self._total_chat_lines - self.get_visible_window_height())
        if delta < 0:
            self._auto_scroll = False
            self._chat_scroll_top = max(0, current + delta)
        elif current + delta >= max_scroll:
            self._auto_scroll = True
            self._chat_scroll_top = max_scroll
        else:
            self._chat_scroll_top = current + delta

    def on_chat_mouse(self, mouse_event: MouseEvent) -> object:
        event_type = mouse_event.event_type
        if event_type == MouseEventType.SCROLL_UP:
            self._scroll_by(-3)
            return None
        if event_type == MouseEventType.SCROLL_DOWN:
            self._scroll_by(3)
            return None
        if not self.selection_enabled:
            return NotImplemented
        x, y = mouse_event.position.x, mouse_event.position.y
        if event_type == MouseEventType.MOUSE_DOWN:
            if mouse_event.button in (MouseButton.LEFT, MouseButton.UNKNOWN):
                return self.begin_selection(x, y)
            return NotImplemented
        if event_type == MouseEventType.MOUSE_MOVE:
            # Motion is only a drag while a button is held (terminals in
            # any-motion mode also report plain hovering).
            if mouse_event.button in (MouseButton.LEFT, MouseButton.UNKNOWN):
                return self.drag_selection(x, y)
            return NotImplemented
        if event_type == MouseEventType.MOUSE_UP:
            return self.finish_selection()
        return NotImplemented

    # ── Drag-to-select (see ``selection.py``) ──────────────────────────────
    #
    # A press in the pane arms a selection, dragging extends it (auto-
    # scrolling past the top/bottom edge) and the release copies its text.
    # Points are ``(transcript line, cell)``, so the range survives scrolling
    # and appended blocks; anything that re-lays the lines out drops it
    # (:meth:`_selection_is_stale`).

    @property
    def selection_enabled(self) -> bool:
        config = getattr(self.app, "_config", None)
        return bool(getattr(config, "mouse_selection", True))

    @property
    def selection(self) -> Selection | None:
        return self._selection

    @property
    def selection_pending(self) -> bool:
        """A button went down in the pane and has not been released yet."""
        return self._sel_anchor is not None

    def _window_origin(self) -> tuple[int, int]:
        """Screen ``(x, y)`` of the pane's top-left cell."""
        info = getattr(getattr(self.app, "_chat_window", None), "render_info", None)
        return getattr(info, "_x_offset", 0), getattr(info, "_y_offset", 0)

    def _current_top(self) -> int:
        """Transcript line shown on the pane's first row (as ``render_chat``)."""
        height = self.get_visible_window_height()
        total = self._total_chat_lines
        scroll = self.get_effective_scroll()
        return max(0, min(scroll, total - height)) if total >= height else 0

    def _point_at(self, x: int, y: int) -> tuple[int, int]:
        """The ``(transcript line, cell)`` under screen position ``(x, y)``,
        clamped into the pane."""
        left, top_row = self._window_origin()
        row = max(0, min(y - top_row, self.get_visible_window_height() - 1))
        line = max(0, min(self._current_top() + row, self._total_chat_lines - 1))
        return line, max(0, x - left)

    def _line_ansi(self, line: int) -> str:
        """Rendered ANSI of transcript line *line* (from the cached text)."""
        return windowed_slice_parts(
            self._win_prefix,
            self._win_prefix_bounds,
            self._win_tail,
            self._win_tail_bounds,
            line,
            1,
        )

    def _layout_fingerprint(self) -> tuple[object, ...]:
        """What must stay the same for selected lines to mean the same text."""
        return (
            self._block_ansi_cache,
            self._block_ansi_cache.generation,
            self._last_width,
            getattr(self.sink.blocks, "rewrite_version", None),
        )

    def begin_selection(self, x: int, y: int) -> object:
        """Button down in the pane: drop the old selection, arm a new one.

        A second / third press on the same cell selects the word / the line.
        Returns ``NotImplemented`` when nothing on screen changed.
        """
        had_selection = self._selection is not None
        point = self._point_at(x, y)
        now = time.monotonic()
        last_at, last_point, clicks = self._sel_click
        repeat = point == last_point and now - last_at <= _MULTI_CLICK_SECONDS
        clicks = clicks % 3 + 1 if repeat else 1
        self._sel_click = (now, point, clicks)
        self._selection = None
        self._sel_anchor = point
        self._sel_dragging = False
        self._sel_edge = 0
        self._sel_fingerprint = self._layout_fingerprint()
        if clicks > 1:
            line, cell = point
            text = plain_line(self._line_ansi(line))
            cells = (
                word_cells(text, cell)
                if clicks == 2
                else (0, max(0, cell_width(text.rstrip()) - 1))
            )
            if cells is not None and text.strip():
                self._selection = Selection((line, cells[0]), (line, cells[1]))
                self._sel_dragging = True  # copied on release, like a drag
        return None if had_selection or self._selection else NotImplemented

    def drag_selection(self, x: int, y: int) -> object:
        """Motion with the button held: extend the selection to ``(x, y)``.

        Past the pane's top or bottom edge the chat scrolls one line per
        event, and keeps scrolling on a timer while the pointer stays there.
        """
        anchor = self._sel_anchor
        if anchor is None:
            return NotImplemented
        _, top_row = self._window_origin()
        height = self.get_visible_window_height()
        self._sel_edge = -1 if y < top_row else 1 if y >= top_row + height else 0
        self._sel_y = y
        if self._sel_edge:
            # Past an edge the selection runs to the start of the top line
            # or the end of the bottom one, whatever column the pointer is in.
            self._sel_x = x = 0 if self._sel_edge < 0 else _END_OF_LINE
            self._scroll_by(self._sel_edge)
            self._start_edge_scroll()
        point = self._point_at(x, y)
        if not self._sel_dragging and point == anchor:
            return NotImplemented  # a click that has not moved off its cell
        self._sel_dragging = True
        self._selection = Selection(anchor, point)
        return None

    def finish_selection(self) -> object:
        """Button released: copy the dragged text; a plain click copies nothing."""
        if self._sel_anchor is None:
            return NotImplemented
        dragged = self._sel_dragging
        self._sel_anchor = None
        self._sel_dragging = False
        self._sel_edge = 0
        selection = self._selection
        if not dragged or selection is None:
            return NotImplemented
        self._sel_epoch = self._chat_content_epoch
        text = selected_text(self._line_ansi, selection)
        if not text.strip():
            self._selection = None  # only padding was dragged over
            return None
        # Blank lines at the end (a drag released below the last line of
        # text) are neither copied nor kept selected: the line after the
        # transcript is where the next output lands.
        lines = text.split("\n")
        blank = next(i for i, line in enumerate(reversed(lines)) if line)
        if blank:
            start, (last, _) = selection.ordered()
            text = "\n".join(lines[:-blank])
            self._selection = Selection(start, (last - blank, _END_OF_LINE))
        copy = getattr(self.app, "copy_selection", None)
        if callable(copy):
            copy(text)
        return None

    def clear_selection(self) -> bool:
        """Drop the selection (and any drag in progress); True if one showed."""
        had_selection = self._selection is not None
        self._selection = None
        self._sel_anchor = None
        self._sel_dragging = False
        self._sel_edge = 0
        if had_selection and self.app is not None:
            self.invalidate()
        return had_selection

    def _start_edge_scroll(self) -> None:
        """Keep scrolling while the pointer rests past an edge mid-drag."""
        if self._sel_edge_task is not None and not self._sel_edge_task.done():
            return
        application = getattr(self.app, "app", None)
        if application is None or not getattr(application, "is_running", False):
            return

        async def _scroll() -> None:
            while self._sel_edge and self._sel_anchor is not None:
                await asyncio.sleep(_EDGE_SCROLL_SECONDS)
                if not self._sel_edge or self._sel_anchor is None:
                    break
                self._scroll_by(self._sel_edge)
                self._selection = Selection(
                    self._sel_anchor, self._point_at(self._sel_x, self._sel_y)
                )
                self.invalidate()

        self._sel_edge_task = application.create_background_task(_scroll())

    def _selection_is_stale(self, selection: Selection) -> bool:
        """True when the selected lines no longer hold the selected text.

        Appending blocks keeps earlier lines where they were, so a selection
        in the settled transcript survives new output. It is dropped when the
        lines are laid out again (resize, theme, an edited or cleared
        transcript) or when it reaches into the in-flight turn, whose lines
        are rewritten on every frame.
        """
        if self._sel_fingerprint != self._layout_fingerprint():
            return True
        (_, _), (last, _) = selection.ordered()
        if last >= self._total_chat_lines:
            return True
        if self._sel_anchor is not None:
            return False  # still dragging: the text is read on release
        in_flight_from = len(self._win_prefix_bounds) - 1
        return last >= in_flight_from and self._sel_epoch != self._chat_content_epoch

    def _with_selection(self, window: ANSI, top: int) -> AnyFormattedText:
        """*window* with the selection highlighted (cached per frame input)."""
        selection = self._selection
        if selection is None:
            return window
        if self._selection_is_stale(selection):
            self._selection = None
            self._sel_anchor = None
            self._sel_dragging = False
            return window
        key = (top, selection)
        if window is not self._sel_overlay_source or key != self._sel_overlay_key:
            self._sel_overlay = FormattedText(
                highlight_window(to_formatted_text(window), top, selection)
            )
            self._sel_overlay_source = window
            self._sel_overlay_key = key
        return self._sel_overlay

    # ── Rendering / windowing ──────────────────────────────────────────────

    def compute_chat_bounds(self, text: str, prefix_len: int, width: int) -> list[int]:
        """Per-line char offsets for *text*, built incrementally (T-14 follow-up).

        Returns the same list :func:`render._line_boundaries` would produce
        over the whole string — ``bounds[k]`` = start of visual line ``k`` —
        but without re-scanning the frozen prefix every frame. The transcript
        is ``text[:prefix_len]`` (frozen, the concatenated immutable blocks)
        + ``text[prefix_len:]`` (the in-flight tail: activity line, streaming
        panel, subagent panel). While a turn streams, only the tail changes,
        so the frozen prefix's line boundaries are cached and only the small
        tail is re-scanned, then spliced on.

        To be precise about what this buys (F-44): every dirty frame still
        assembles the full transcript string and still copies
        ``text[:prefix_len]`` / ``text[prefix_len:]`` — those are C-speed
        ``memcpy``. The win is that the *line-bounds build* (a Python
        ``str.find`` loop, i.e. one Python iteration per line) runs over the
        tail only, so per dirty frame it is O(visible) instead of
        O(transcript) during streaming.

        The cache is invalidated when the frozen prefix changes: the
        transcript is append-only, every in-place block edit replaces the
        block with a *new* object (so its ``id`` changes), and a width change
        re-renders every block — all captured by the fingerprint below. On a
        cache miss the prefix bounds are rebuilt from the (C-speed) prefix
        scan.

        The fingerprint also carries the block ANSI cache's *generation*
        (bumped on every ``clear``): ``apply_theme`` and ``_reset_transcript``
        drop the cache while the transcript may keep the same block objects,
        and after a refill the same ids can describe different ANSI strings
        (a theme with differently-long escapes), so ids alone would be a
        stale fingerprint (F-41).

        The steady-state render path no longer calls this (it keeps prefix
        and tail apart, see :meth:`_frozen_bounds_for`); it remains the
        full-string API for callers that hold one concatenated text.
        """
        tail = text[prefix_len:]
        fingerprint = self._prefix_fingerprint(width)
        if fingerprint != self._frozen_ansi_ids:
            prefix = text[:prefix_len]
            self._frozen_ansi_bounds = _line_boundaries(prefix)
            self._frozen_ansi_ids = fingerprint
            self._frozen_prefix_text = prefix
        # Splice: the prefix's lines, then the tail's lines. The frozen prefix
        # ends in a newline (Rich re-asserts SGR + a \n after every line), so
        # the tail always begins on a fresh visual line whose start offset
        # coincides with the prefix's trailing-empty-line start; dropping
        # ``tail_bounds[0]`` (=0, the prefix's last line) avoids double-counting
        # it and shifts the tail's offsets by ``prefix_len``.
        tail_bounds = _line_boundaries(tail)
        if len(tail_bounds) == 1:
            return self._frozen_ansi_bounds
        return self._frozen_ansi_bounds + [prefix_len + b for b in tail_bounds[1:]]

    def _prefix_fingerprint(self, width: int) -> tuple[object, ...]:
        """Identity of the frozen prefix's inputs, for the bounds cache.

        Always carries the block-ANSI cache *generation* (F-41: a cleared and
        refilled cache can give the same blocks different escapes) and the
        width. The block set is identified in O(1) by the sink's
        ``BlockList.version`` (bumped on every mutation, never reused), so a
        streaming frame no longer builds a tuple of every block id. A sink
        whose ``blocks`` is a plain list (test stubs) falls back to the
        per-block ids.
        """
        blocks = self.sink.blocks
        version = getattr(blocks, "version", None)
        if version is not None:
            return (self._block_ansi_cache.generation, width, "v", version)
        return (
            self._block_ansi_cache.generation,
            width,
            *(id(block) for block in blocks),
        )

    def _frozen_bounds_for(self, prefix: str, width: int) -> list[int]:
        """Line starts of the frozen *prefix*, cached across frames.

        * **Hit** — same fingerprint (see :meth:`_prefix_fingerprint`) *and*
          the very same prefix string object as last frame (the render cache
          returns the identical object while nothing changed): O(1). The
          string identity alone already guarantees the bounds are right
          (``str`` is immutable); the fingerprint is belt and braces.
        * **Append** — the new prefix starts with the previously scanned
          one (blocks appended): only the new suffix is scanned and its
          offsets spliced on. This is content-checked (``startswith``), so
          it is immune to the F-41 stale-id problem.
        * **Rebuild** — anything else (edit, clear, theme change, resize).

        The append and rebuild paths always produce a *new* list object.
        """
        fingerprint = self._prefix_fingerprint(width)
        old = self._frozen_prefix_text
        if prefix is old and fingerprint == self._frozen_ansi_ids:
            return self._frozen_ansi_bounds
        if old is not None and len(prefix) >= len(old) and prefix.startswith(old):
            base = len(old)
            bounds = self._frozen_ansi_bounds + [
                base + b for b in _line_boundaries(prefix[base:])[1:]
            ]
        else:
            bounds = _line_boundaries(prefix)
        self._frozen_ansi_bounds = bounds
        self._frozen_ansi_ids = fingerprint
        self._frozen_prefix_text = prefix
        return bounds

    def _debounced_width(self, width: int) -> int:
        """The width to lay out at this frame: *width*, or the previous one
        while a resize of a long transcript is still settling.

        A width change re-renders every block (the caches are per width),
        which for a long transcript costs seconds — and dragging a window
        edge changes the width once per column. So the new width is only
        adopted after it has been stable for :data:`_RESIZE_DEBOUNCE_SECONDS`;
        until then the old layout stays on screen (cropped or short of the
        new edge) and a timer repaints when the wait is over. Short
        transcripts, and hosts with no running app, re-lay out at once.
        """
        if width == self._last_width or self._chat_content_epoch == 0:
            self._resize_width = 0
            return width
        if self._total_chat_lines < _RESIZE_DEBOUNCE_MIN_LINES:
            return width
        application = getattr(self.app, "app", None)
        if application is None or not getattr(application, "is_running", False):
            return width
        now = time.monotonic()
        if width != self._resize_width:
            self._resize_width = width
            self._resize_since = now
        remaining = _RESIZE_DEBOUNCE_SECONDS - (now - self._resize_since)
        if remaining <= 0:
            return width
        if self._resize_timer is not None:
            self._resize_timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return width
        self._resize_timer = loop.call_later(remaining, self.invalidate)
        return self._last_width

    def render_chat(self) -> AnyFormattedText:
        term_width = shutil.get_terminal_size((80, 24)).columns
        width = self._debounced_width(max(40, term_width - 4))

        # render_ms: wall time of this frame's full re-render (0.0 when the
        # frame only re-sliced an already-rendered transcript, e.g. scroll).
        render_ms = 0.0
        if self.sink.dirty or width != self._last_width:
            render_start = time.perf_counter()
            # TODO(T-15): wire BlockFormattedTextCache into render_chat —
            # window the cached FormattedText fragments directly so the pane
            # skips the per-frame ANSI re-parse. Not done yet on purpose:
            # the ANSI-string windowing below relies on Rich re-asserting
            # each line's SGR state after every newline, and a
            # fragments->ANSI_to_string round trip would drop that
            # re-assertion and unstyle the window's top line. The FT cache
            # is already created/cleared alongside the ANSI cache (init,
            # apply_theme, _reset_transcript), so the switch is a
            # fragment-windowing change in this method + render_chat_parts.
            # It is also not a scaling issue: only the *visible window* is
            # wrapped in ``ANSI(...)`` below, so the per-frame parse is
            # O(visible) regardless of transcript length.
            prefix, tail = render_chat_parts(self.sink, width, self._block_ansi_cache)
            # T-14 (#171): the transcript is kept as frozen prefix + in-flight
            # tail. Rich re-asserts each line's SGR state after every newline,
            # so the text can be sliced at any line boundary (see
            # render._line_boundaries / windowed_slice_parts) without
            # corrupting styling. While a turn streams the prefix is the same
            # cached string object, so its bounds are an O(1) cache hit; only
            # the small tail is re-scanned, and the two are never joined here
            # — the per-frame cost is O(tail). Total lines match
            # ``count("\n") + 1`` of the concatenation (ptk's ``split_lines``).
            prefix_bounds = self._frozen_bounds_for(prefix, width)
            tail_bounds = _line_boundaries(tail)
            self._win_prefix = prefix
            self._win_prefix_bounds = prefix_bounds
            self._win_tail = tail
            self._win_tail_bounds = tail_bounds
            self._full_text_cache = None
            self._full_bounds_cache = None
            self._total_chat_lines = max(1, len(prefix_bounds) + len(tail_bounds) - 1)
            self._chat_content_epoch += 1
            self.sink.dirty = False
            self._last_width = width
            render_ms = (time.perf_counter() - render_start) * 1e3

        height = self.get_visible_window_height()
        total = self._total_chat_lines
        scroll = self.get_effective_scroll()
        top = max(0, min(scroll, total - height)) if total >= height else 0

        # Slice out the visible window. Cache the result: scrolling (which
        # does NOT change the transcript) only changes ``top``, so the slice is
        # O(visible) string work rather than an O(transcript) re-render.
        # ``_chat_content_epoch`` must also be part of the key: dirty frames
        # (spinner tick, streamed token) change the *content* at the same
        # (top, height, total), and a same-position slice of the stale string
        # would keep the old glyph — which is exactly what froze the
        # in-chat spinner after the windowing landed.
        if (
            top != self._window_top
            or height != self._window_height
            or total != self._window_total
            or self._chat_content_epoch != self._window_epoch
        ):
            slice_start = time.perf_counter()
            self._windowed_ansi = ANSI(
                windowed_slice_parts(
                    self._win_prefix,
                    self._win_prefix_bounds,
                    self._win_tail,
                    self._win_tail_bounds,
                    top,
                    height,
                )
            )
            slice_ms = (time.perf_counter() - slice_start) * 1e3
            self._window_top = top
            self._window_height = height
            self._window_total = total
            self._window_epoch = self._chat_content_epoch
            _chat_perf_log(
                full_chars=len(self._win_prefix) + len(self._win_tail),
                total_lines=total,
                top=top,
                height=height,
                slice_ms=slice_ms,
                render_ms=render_ms,
            )
        # The highlight is an overlay on this window only: the cached lines
        # (and the per-block render cache behind them) are never touched.
        return self._with_selection(self._windowed_ansi, top)

    def lines_below(self) -> int:
        """Transcript lines below the visible window (0 when following)."""
        visible = self.get_visible_window_height()
        return max(0, self._total_chat_lines - self.get_effective_scroll() - visible)

    def scrollbar_state(self) -> tuple[int, int]:
        """(total_lines, scroll_top) for :class:`ChatScrollbarMargin`.

        Returns the *logical* scroll of the full transcript (not the windowed
        content, which always equals the visible height). The margin computes
        the thumb from these against the real transcript length.
        """
        return self._total_chat_lines, self.get_effective_scroll()
