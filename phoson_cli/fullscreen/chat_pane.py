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
import logging
from typing import Any
from collections.abc import Callable

from prompt_toolkit import Application
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.containers import Window

from .render import (
    BlockAnsiCache,
    BlockFormattedTextCache,
    windowed_slice,
    _line_boundaries,
    render_chat_split,
)

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
      frames that only re-sliced (scroll). Its *bounds* component is
      O(visible) (the frozen prefix's line offsets are cached; only the small
      in-flight tail is re-scanned — see :meth:`ChatPane.compute_chat_bounds`),
      but it also includes re-rendering the frozen transcript, which is still
      O(transcript); that term is the remaining cost to eliminate for a fully
      O(visible) streaming frame (known follow-up).

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
        if total <= 0 or height <= 0:
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
        # T-14 (#171) — windowed chat render. The whole transcript is cached
        # as ONE ANSI string (re-rendered only when dirty/resized), but
        # prompt_toolkit is handed only the *visible window* substring.
        self._full_ansi_text = ""
        self._full_ansi_bounds: list[int] = [0]
        # T-14 follow-up: incremental line-bounds for the frozen prefix.
        self._frozen_ansi_bounds: list[int] = [0]
        self._frozen_ansi_ids: tuple[int, ...] | None = None
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

    # ── Lazy views onto the owning app ─────────────────────────────────────

    @property
    def sink(self) -> Any:
        if self._sink_override is not None:
            return self._sink_override
        return self.app.sink

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

    def on_chat_mouse(self, mouse_event: MouseEvent) -> object:
        current = self.get_effective_scroll()
        max_scroll = max(0, self._total_chat_lines - self.get_visible_window_height())
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._auto_scroll = False
            self._chat_scroll_top = max(0, current - 3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            if current + 3 >= max_scroll:
                self._auto_scroll = True
                self._chat_scroll_top = max_scroll
            else:
                self._chat_scroll_top = current + 3
            return None
        return NotImplemented

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
        """
        prefix = text[:prefix_len]
        tail = text[prefix_len:]
        fingerprint = (
            self._block_ansi_cache.generation,
            width,
            *(id(block) for block in self.sink.blocks),
        )
        if fingerprint != self._frozen_ansi_ids:
            self._frozen_ansi_bounds = _line_boundaries(prefix)
            self._frozen_ansi_ids = fingerprint
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

    def render_chat(self) -> ANSI:
        term_width = shutil.get_terminal_size((80, 24)).columns
        width = max(40, term_width - 4)

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
            # fragment-windowing change in this method + render_chat_split.
            text, prefix_len = render_chat_split(
                self.sink, width, self._block_ansi_cache
            )
            # T-14 (#171): cache the *full* transcript as one ANSI string.
            # Rich re-asserts each line's SGR state after every newline, so the
            # string can later be sliced at any line boundary (see
            # render._line_boundaries / windowed_slice) without corrupting
            # styling. ``compute_chat_bounds`` builds the per-line char offsets
            # incrementally: the frozen prefix's bounds are cached and only the
            # small in-flight tail is re-scanned, so the full-string bounds
            # match ``count("\n") + 1`` lines (matching ptk's ``split_lines``).
            self._full_ansi_text = text
            self._full_ansi_bounds = self.compute_chat_bounds(text, prefix_len, width)
            self._total_chat_lines = max(1, len(self._full_ansi_bounds))
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
                windowed_slice(
                    self._full_ansi_text, self._full_ansi_bounds, top, height
                )
            )
            slice_ms = (time.perf_counter() - slice_start) * 1e3
            self._window_top = top
            self._window_height = height
            self._window_total = total
            self._window_epoch = self._chat_content_epoch
            _chat_perf_log(
                full_chars=len(self._full_ansi_text),
                total_lines=total,
                top=top,
                height=height,
                slice_ms=slice_ms,
                render_ms=render_ms,
            )
        return self._windowed_ansi

    def scrollbar_state(self) -> tuple[int, int]:
        """(total_lines, scroll_top) for :class:`ChatScrollbarMargin`.

        Returns the *logical* scroll of the full transcript (not the windowed
        content, which always equals the visible height). The margin computes
        the thumb from these against the real transcript length.
        """
        return self._total_chat_lines, self.get_effective_scroll()
