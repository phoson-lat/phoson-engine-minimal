"""Tests for T-14 (#171): windowed chat render.

The chat pane caches the *whole* transcript as one ANSI string but only hands
prompt_toolkit the visible window (``PhosonApp._render_chat``). These tests
cover:

* ``render._line_boundaries`` / ``render.windowed_slice`` — the slicing
  primitive, including the case that motivated it (a window reaching the last
  line of a transcript that does *not* end in a newline must keep that line).
* render-equivalence: slicing at a line boundary and re-parsing yields the
  same *visible text* as parsing the full string.
* the app wiring: window size, auto-scroll shows the tail, scroll-up shows a
  middle slice, ``scroll_home`` shows the top, short transcripts clamp, the
  (hidden) cursor sits at y=0, and the logical scroll feeds the scrollbar.
* ``ChatScrollbarMargin`` — the thumb tracks the logical scroll (top/mid/
  bottom), fills the bar for short transcripts, and is empty for none.
"""

import os
import shutil
from unittest.mock import MagicMock, patch

from prompt_toolkit.formatted_text import (
    ANSI,
    split_lines,
    to_formatted_text,
    fragment_list_to_text,
)

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_cli.fullscreen.render import windowed_slice, _line_boundaries


def _app_for(tmp_path, **config_kwargs):
    """A bare PhosonApp (mocked chat client) — no run loop."""
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            **config_kwargs,
        )
        from phoson_cli.fullscreen.app import PhosonApp

        return PhosonApp(config)


def _set_term(app, columns=120, lines=30) -> None:
    """Pin the terminal size the app reads (the fallback when ptk has no
    render_info yet, i.e. outside a live run loop)."""
    shutil.get_terminal_size = lambda fallback=(80, 24): os.terminal_size(
        (columns, lines)
    )


def _push_long_transcript(app, n: int) -> None:
    """Seed the sink with *n* alternating user/assistant turns (via the real
    history path, so the block cache is exercised)."""
    msgs = [
        Message(role=("user" if i % 2 == 0 else "assistant"), content=f"msg {i}")
        for i in range(n)
    ]
    app.sink.on_user_message("msg 0", msgs[0])
    app.sink.print_history(msgs)


# ── _line_boundaries ───────────────────────────────────────────────────────────


def test_line_boundaries_no_trailing_newline() -> None:
    # Each bound is the start offset of a line; len == ptk line count.
    assert _line_boundaries("a\nb\nc\nd") == [0, 2, 4, 6]
    assert len(_line_boundaries("a\nb\nc\nd")) == 4


def test_line_boundaries_trailing_newline() -> None:
    # A trailing \n adds one empty final line (ptk's split_lines does the
    # same), so the count is 4, matching split_lines.
    s = "a\nb\nc\n"
    bounds = _line_boundaries(s)
    assert len(bounds) == 4
    assert list(split_lines(to_formatted_text(ANSI(s)))) and (
        len(list(split_lines(to_formatted_text(ANSI(s))))) == len(bounds)
    )


def test_line_boundaries_single_and_empty() -> None:
    assert _line_boundaries("x") == [0]
    assert _line_boundaries("") == [0]


def test_line_boundaries_match_ptk_line_count() -> None:
    for s in ["", "x", "a\nb", "a\nb\n", "a\n\nb", "a\nb\nc\nd\n", "one\ntwo\n"]:
        ptk = len(list(split_lines(to_formatted_text(ANSI(s)))))
        assert len(_line_boundaries(s)) == ptk, f"mismatch for {s!r}"


# ── windowed_slice ──────────────────────────────────────────────────────────────


def _visible(s: str) -> list[str]:
    """Visible text lines of an ANSI string, minus ptk's trailing empty line."""
    lines = [
        fragment_list_to_text(line) for line in split_lines(to_formatted_text(ANSI(s)))
    ]
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


def test_windowed_slice_bottom_keeps_final_line() -> None:
    """The motivating bug: a transcript that does NOT end in a newline — the
    last line's start is bounds[-1], its end is the string end (not a further
    boundary). A bottom window must still render that final line."""
    s = "a\nb\nc\nd"
    bounds = _line_boundaries(s)
    # Window the last 2 lines.
    assert _visible(windowed_slice(s, bounds, 2, 2)) == ["c", "d"]
    # The whole transcript.
    assert _visible(windowed_slice(s, bounds, 0, 4)) == ["a", "b", "c", "d"]
    # A window that over-reaches the bottom clamps to the end.
    assert _visible(windowed_slice(s, bounds, 3, 99)) == ["d"]


def test_windowed_slice_middle_window() -> None:
    s = "\n".join(str(i) for i in range(10))  # 0..9
    bounds = _line_boundaries(s)
    assert _visible(windowed_slice(s, bounds, 3, 4)) == ["3", "4", "5", "6"]


def test_windowed_slice_clamps_out_of_range() -> None:
    s = "\n".join(str(i) for i in range(10))
    bounds = _line_boundaries(s)
    assert windowed_slice(s, bounds, -5, 3) == windowed_slice(s, bounds, 0, 3)
    assert _visible(windowed_slice(s, bounds, 99, 3)) == []
    assert windowed_slice(s, bounds, 0, 0) == ""
    assert windowed_slice("", [0], 0, 3) == ""


def test_windowed_slice_render_equivalence_random() -> None:
    """For many random (top, height) pairs the slice re-parses to exactly the
    full string's visible lines [top:top+height]."""
    s = "\n".join(f"line {i}" for i in range(120))
    bounds = _line_boundaries(s)
    full = _visible(s)
    total = len(bounds)
    import random

    random.seed(171)
    for _ in range(1000):
        top = random.randint(0, total)
        height = random.randint(0, total + 1)
        got = _visible(windowed_slice(s, bounds, top, height))
        assert got == full[top : top + height], f"top={top} height={height}"


# ── PhosonApp._render_chat windowing ────────────────────────────────────────────


def test_render_chat_windows_to_visible_height(tmp_path) -> None:
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    vh = app._get_visible_window_height()
    # The control only ever holds the visible window (<= viewport height).
    rendered = _visible(app._windowed_ansi.value)
    assert len(rendered) <= vh
    # The full transcript is much taller than one window.
    assert app._total_chat_lines > vh


def test_render_chat_auto_scroll_shows_tail(tmp_path) -> None:
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    rendered = [line for line in _visible(app._windowed_ansi.value) if line.strip()]
    # The most recent turn is at the bottom of the auto-scrolled window.
    assert any("msg 199" in line for line in rendered)
    # The earliest turns are NOT in the window (scrolled off the top).
    assert not any("msg 0 " in line for line in rendered)


def test_render_chat_scroll_up_shows_middle(tmp_path) -> None:
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    total = app._total_chat_lines
    # Scroll to a fixed position in the middle of the transcript.
    target = total // 2
    app._auto_scroll = False
    app._chat_scroll_top = target
    app.app.invalidate()
    app._render_chat()

    # The window starts at line `target`.
    assert app._window_top == target
    rendered = [line for line in _visible(app._windowed_ansi.value) if line.strip()]
    # Neither the very bottom nor the very top is visible at mid-transcript.
    assert not any("msg 199" in line for line in rendered)
    assert len(rendered) >= 1


def test_render_chat_scroll_home_shows_top(tmp_path) -> None:
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()
    app.scroll_home()
    app.app.invalidate()
    app._render_chat()

    assert app._window_top == 0
    # Auto-scroll is off after a manual scroll.
    assert app._auto_scroll is False


def test_render_chat_short_transcript_clamps(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.sink.on_user_message("only", Message(role="user", content="only"))
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    # Far shorter than the window: no over-scroll, cursor stays at 0,
    # effective scroll clamps to 0.
    assert app._total_chat_lines < app._get_visible_window_height()
    assert app._window_top == 0
    assert app._get_effective_scroll() == 0
    assert "only" in app._windowed_ansi.value


def test_render_chat_cursor_at_top_of_slice(tmp_path) -> None:
    """With a windowed slice the (hidden) cursor sits at y=0 so ptk keeps the
    window's own vertical scroll at 0 (the logical scroll is ours)."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    from prompt_toolkit.data_structures import Point

    assert app._get_chat_cursor_position() == Point(x=0, y=0)


def test_render_chat_scrollbar_state_tracks_logical_scroll(tmp_path) -> None:
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    # Auto-scroll: the logical scroll is at the bottom (max_scroll).
    vh = app._get_visible_window_height()
    max_scroll = max(0, app._total_chat_lines - vh)
    assert app._scrollbar_state() == (app._total_chat_lines, max_scroll)

    # A manual scroll to the top feeds the top to the scrollbar.
    app.scroll_home()
    app.app.invalidate()
    assert app._scrollbar_state() == (app._total_chat_lines, 0)


def test_render_chat_is_cached_until_dirty(tmp_path) -> None:
    """Scrolling (which does not dirty the transcript) must not re-render the
    full transcript; it only re-slices. The full string object is stable."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 200)
    _set_term(app, columns=120, lines=30)
    app._render_chat()
    full_before = app._full_ansi_text

    app.scroll_line_up()
    app.app.invalidate()
    app._render_chat()
    assert app._full_ansi_text is full_before  # not re-rendered
    assert app.sink.dirty is False


# ── T-14 incremental bounds (render_chat_split + _compute_chat_bounds) ─────────


def _streaming_turn(app, content: str) -> None:
    """Start an in-flight turn and set its streamed text (a dirty frame's tail)."""
    app.sink.begin_activity()
    app.sink.current_turn.content = content


def test_compute_chat_bounds_matches_full_scan(tmp_path) -> None:
    """The incremental builder returns exactly what a full `_line_boundaries`
    scan would — across a frozen prefix + streaming tail, incl. a tail with no
    trailing newline (which merges into the prefix's final line)."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 40)
    _set_term(app, columns=120, lines=30)
    for tail in ("", "more text\n", "more\ntext without trailing newline"):
        _streaming_turn(app, tail)
        app._render_chat()
        expected = _line_boundaries(app._full_ansi_text)
        assert app._full_ansi_bounds == expected, f"tail={tail!r}"
        # Line count drives the scrollbar + windowing — must match ptk's.
        assert app._total_chat_lines == len(expected)


def test_compute_chat_bounds_caches_prefix_while_streaming(tmp_path) -> None:
    """Streaming (same frozen blocks, growing tail) must reuse the cached
    prefix bounds — the prefix is not re-scanned, which is what keeps the
    per-frame line-bounds cost O(visible) instead of O(transcript)."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 60)
    _set_term(app, columns=120, lines=30)
    _streaming_turn(app, "first token\n")
    app._render_chat()
    prefix_bounds_before = app._frozen_ansi_bounds
    ids_before = app._frozen_ansi_ids
    assert ids_before is not None

    # Stream more content: same blocks, only the tail grows.
    for chunk in ("first\nsecond token\n", "first\nsecond\nthird line\n"):
        app.sink.current_turn.content = chunk
        app._render_chat()
        # The frozen prefix's block set is unchanged → fingerprint matches →
        # the cached prefix bounds object is reused (no re-scan).
        assert app._frozen_ansi_ids == ids_before
        assert app._frozen_ansi_bounds is prefix_bounds_before
        # And the spliced result is still correct.
        assert app._full_ansi_bounds == _line_boundaries(app._full_ansi_text)


def test_compute_chat_bounds_invalidates_on_block_change(tmp_path) -> None:
    """A new block changes the frozen prefix → the fingerprint changes → the
    prefix bounds are rebuilt (a different object), and the result is correct."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 40)
    _set_term(app, columns=120, lines=30)
    _streaming_turn(app, "x\n")
    app._render_chat()
    prefix_before = app._frozen_ansi_bounds
    ids_before = app._frozen_ansi_ids

    # A new user message appends a block (frozen prefix grows).
    app.sink.on_user_message("another", Message(role="user", content="another"))
    app._render_chat()
    assert app._frozen_ansi_ids != ids_before
    assert app._frozen_ansi_bounds is not prefix_before
    assert app._full_ansi_bounds == _line_boundaries(app._full_ansi_text)


def test_cache_generation_bumped_on_clear() -> None:
    """F-41 (#186): the fingerprint of the frozen-prefix bounds cache includes
    the block-ANSI cache's generation, which must bump on every ``clear`` —
    otherwise a cleared+refilled cache (theme change, transcript reset) with
    the same block ids but different escapes would hit stale bounds."""
    from phoson_cli.fullscreen.render import BlockAnsiCache

    cache = BlockAnsiCache()
    gen0 = cache.generation
    cache.clear(80)
    assert cache.generation == gen0 + 1
    cache.clear(0)
    assert cache.generation == gen0 + 2


def test_apply_theme_invalidates_frozen_bounds_cache(tmp_path) -> None:
    """F-41 (#186): ``apply_theme`` clears the block ANSI cache, so the
    frozen-prefix bounds must be rebuilt on the next frame — the fingerprint
    changes even though the *same* block objects remain in the transcript
    (their ids alone would not detect the cache invalidation, and a theme
    with differently-long escapes would render different line counts)."""
    from phoson_cli.theme import DARK, LIGHT

    app = _app_for(tmp_path)
    _push_long_transcript(app, 40)
    _set_term(app, columns=120, lines=30)
    app._render_chat()
    fingerprint_before = app._frozen_ansi_ids
    bounds_before = app._frozen_ansi_bounds
    assert fingerprint_before is not None

    app.apply_theme(LIGHT)
    app._render_chat()

    # The cache generation bump (via apply_theme → clear) changed the
    # fingerprint, so the prefix bounds were rebuilt, not served from cache.
    assert app._frozen_ansi_ids != fingerprint_before
    assert app._frozen_ansi_bounds is not bounds_before
    # And the rebuilt bounds are still correct.
    assert app._full_ansi_bounds == _line_boundaries(app._full_ansi_text)

    # Back to the original theme: same blocks again, but the generation has
    # moved on, so this also rebuilds (no stale hit across two clears).
    app.apply_theme(DARK)
    app._render_chat()
    assert app._frozen_ansi_bounds is not bounds_before
    assert app._full_ansi_bounds == _line_boundaries(app._full_ansi_text)


def test_render_chat_split_reports_frozen_prefix_length(tmp_path) -> None:
    """render_chat_split's prefix_len is the length of the frozen (no
    in-flight) transcript — it must equal the prefix that would render with
    the current turn removed, so the app can cache its boundaries."""
    from phoson_cli.fullscreen.render import render_chat_split

    app = _app_for(tmp_path)
    _push_long_transcript(app, 20)
    _set_term(app, columns=120, lines=30)
    # Idle: no in-flight turn → the whole transcript is the frozen prefix.
    text, prefix_len = render_chat_split(app.sink, 120, app._block_ansi_cache)
    assert prefix_len == len(text)


def test_spinner_tick_refreshes_same_position_window(tmp_path) -> None:
    """A spinner tick changes the glyph at the *same* (top, height, total):
    the cached visible slice must re-slice the re-rendered transcript.

    Regression: after the T-14 windowing, the slice cache only refreshed on
    (top, height, total) changes, so a spinner tick (identical window shape,
    same line count, new glyph) kept serving the stale fragment and the
    in-chat spinner appeared frozen."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 60)  # longer than the 30-line window
    _set_term(app, columns=120, lines=30)
    app.sink.begin_activity()
    app._render_chat()
    first = app._windowed_ansi.value
    assert app._window_top > 0  # auto-scrolled to the tail
    frame_a = app.sink.activity_frame()
    assert frame_a in first

    # One spinner tick (what _tick_activity_indicators does): new glyph,
    # same window position and line count.
    assert app.sink.tick_activity_frame() is True
    app.sink.dirty = True
    frame_b = app.sink.activity_frame()
    assert frame_b != frame_a
    app._render_chat()

    assert frame_b in app._windowed_ansi.value
    assert frame_a not in app._windowed_ansi.value


# ── ChatScrollbarMargin ─────────────────────────────────────────────────────────


def _thumb_rows(callback, height=25) -> list[int]:
    from phoson_cli.fullscreen.app import ChatScrollbarMargin

    m = ChatScrollbarMargin(callback)
    rows = m.create_margin(None, 1, height)  # rows: (style, char)
    return [i // 2 for i, (style, _ch) in enumerate(rows) if "button" in style]


def test_chat_scrollbar_width_is_one_column() -> None:
    from phoson_cli.fullscreen.app import ChatScrollbarMargin

    assert ChatScrollbarMargin(lambda: (10, 0)).get_width(lambda: None) == 1


def test_chat_scrollbar_thumb_tracks_scroll() -> None:
    total = 305
    top = _thumb_rows(lambda: (total, 0))
    mid = _thumb_rows(lambda: (total, 150))
    bottom = _thumb_rows(lambda: (total, total - 25))

    # A short thumb (window is a small fraction of the transcript).
    assert 0 < len(top) < 25
    # It moves down the bar as scroll increases.
    assert top[0] < mid[0] < bottom[0]
    # Bottom thumb sits at the bottom of the bar.
    assert bottom[-1] >= 25 - len(top) - 1


def test_chat_scrollbar_fills_for_short_transcript() -> None:
    # A transcript no taller than the window: the thumb fills the bar.
    rows = _thumb_rows(lambda: (10, 0), height=25)
    assert len(rows) == 25


def test_chat_scrollbar_empty_when_nothing_to_show() -> None:
    assert _thumb_rows(lambda: (0, 0)) == []


# ── Integration: rewind still shows the rebuilt transcript in-window ────────────


async def test_rewind_redraw_lands_in_window(tmp_path) -> None:
    """After a rewind the pane is rebuilt via print_history; the window must
    auto-scroll to the tail of the rebuilt (shorter) path."""
    app = _app_for(tmp_path)
    _push_long_transcript(app, 60)
    _set_term(app, columns=120, lines=30)
    app._render_chat()

    # Simulate a rewind redraw: clear + re-print a short history.
    app._reset_transcript()
    app.sink.print_history(
        [
            Message(role="user", content="keep one"),
            Message(role="assistant", content="kept reply"),
        ]
    )
    app._auto_scroll = True
    app._chat_scroll_top = 0
    app.app.invalidate()
    app._render_chat()

    assert "keep one" in app._windowed_ansi.value
    assert "kept reply" in app._windowed_ansi.value
