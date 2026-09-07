"""Isolated ChatPane tests for the T-14 windowing bugs (F-40 / F-41).

Issue #187's acceptance criterion: the windowing logic must be unit-testable
**without instantiating ``PhosonApp``**. ``ChatPane`` owns the pane state, so
these tests construct it directly — with a stub sink (and, for the render path,
a tiny stand-in for the ptk ``Window``) — and exercise:

* **F-41** — the frozen-prefix bounds fingerprint includes the block-ANSI
  cache's *generation*, so a cleared+refilled cache (theme change, transcript
  reset) with the *same* block ids but different escapes rebuilds the bounds
  instead of serving a stale hit.
* **F-40** — a window reaching the bottom of a transcript that does *not* end
  in a newline still renders that final line.
* the incremental bounds build (frozen prefix cached, in-flight tail re-scanned
  and spliced) and the effective-scroll / scrollbar plumbing.

No ``PhosonApp`` is constructed anywhere here.
"""

import os
from unittest.mock import patch

from phoson_cli.fullscreen.render import (
    BlockAnsiCache,
    _line_boundaries,
)
from phoson_cli.fullscreen.chat_pane import ChatPane

# ── Stubs (no PhosonApp) ──────────────────────────────────────────────────────


class _StubSink:
    """Just enough of ``FullScreenSink`` for the pane's bounds/render path."""

    def __init__(self, blocks=None) -> None:
        self.blocks = list(blocks) if blocks else []
        self.dirty = False


class _Win:
    """Fake ptk ``Window``: ``render_info`` is ``None`` so the pane falls back
    to the (patched) terminal size for the visible height."""

    render_info = None


class _StubApp:
    """Stand-in for the owning ``PhosonApp`` the pane reads lazily.

    Only what :meth:`ChatPane.render_chat` touches: ``sink`` and
    ``_chat_window``. (``invalidate`` is not on the render path.)
    """

    def __init__(self, sink: _StubSink) -> None:
        self.sink = sink
        self._chat_window = _Win()


def _visible(ansi_value: str) -> list[str]:
    """Visible text lines of an ANSI string, minus ptk's trailing empty line."""
    from prompt_toolkit.formatted_text import (
        ANSI,
        split_lines,
        to_formatted_text,
        fragment_list_to_text,
    )

    lines = [
        fragment_list_to_text(line)
        for line in split_lines(to_formatted_text(ANSI(ansi_value)))
    ]
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


# ── F-41: bounds fingerprint includes the block-ANSI cache generation ─────────


def test_frozen_bounds_cache_hit_on_unchanged_prefix() -> None:
    """Same blocks + width → same fingerprint → cached bounds reused (F-41)."""
    sink = _StubSink(blocks=["block-a", "block-b"])
    pane = ChatPane(sink=sink)
    text = "aaa\nbbb\n"
    pane.compute_chat_bounds(text, prefix_len=len(text), width=80)
    first_ids = pane._frozen_ansi_ids
    first_bounds = pane._frozen_ansi_bounds
    assert first_ids is not None
    assert first_bounds == _line_boundaries(text)

    # No change: the fingerprint is identical, so the same bounds object is
    # kept (a cache hit, not a rebuild).
    pane.compute_chat_bounds(text, prefix_len=len(text), width=80)
    assert pane._frozen_ansi_ids == first_ids
    assert pane._frozen_ansi_bounds is first_bounds


def test_cache_clear_rebuilds_bounds_same_block_ids() -> None:
    """F-41: clearing the block-ANSI cache bumps its generation, so the
    fingerprint changes and the prefix bounds are *rebuilt* even though the
    transcript still holds the same block objects (ids unchanged). This is
    exactly what a theme change / transcript reset must do."""
    sink = _StubSink(blocks=["block-a", "block-b"])
    pane = ChatPane(sink=sink)
    text = "aaa\nbbb\n"
    pane.compute_chat_bounds(text, prefix_len=len(text), width=80)
    first_bounds = pane._frozen_ansi_bounds
    first_ids = pane._frozen_ansi_ids

    # apply_theme / _reset_transcript call clear(0): the generation bumps.
    pane._block_ansi_cache.clear(0)
    assert pane._block_ansi_cache.generation != 0

    pane.compute_chat_bounds(text, prefix_len=len(text), width=80)
    # Same block ids, but the generation is part of the fingerprint, so the
    # prefix bounds object was rebuilt — not the stale first one.
    assert pane._frozen_ansi_ids != first_ids
    assert pane._frozen_ansi_bounds is not first_bounds
    # …and the rebuilt bounds are still correct.
    assert pane._frozen_ansi_bounds == _line_boundaries(text)


# ── Incremental bounds build: frozen prefix cached, tail re-scanned ───────────


def test_incremental_bounds_splice_frozen_and_tail() -> None:
    """The frozen prefix's bounds are cached; only the in-flight tail is
    re-scanned and spliced on, matching a full ``_line_boundaries`` scan."""
    sink = _StubSink(blocks=["block-a"])
    pane = ChatPane(sink=sink)
    frozen = "first line\nsecond line\n"
    tail = "in-flight\n"

    full = frozen + tail
    pane.compute_chat_bounds(full, prefix_len=len(frozen), width=80)
    # The spliced bounds equal the bounds of the whole string.
    assert pane._frozen_ansi_bounds + [
        len(frozen) + b for b in _line_boundaries(tail)[1:]
    ] == _line_boundaries(full)

    # A *newer* tail (a streamed token) at the same frozen prefix: the frozen
    # bounds object is reused (not rebuilt) and only the tail is re-scanned.
    frozen_bounds_before = pane._frozen_ansi_bounds
    tail2 = "in-flight more\n"
    pane.compute_chat_bounds(frozen + tail2, prefix_len=len(frozen), width=80)
    assert pane._frozen_ansi_bounds is frozen_bounds_before


# ── F-40: a bottom window keeps the final non-newline line ────────────────────


def test_render_window_bottom_keeps_final_line() -> None:
    """F-40: a transcript that does NOT end in a newline — the last line's end
    is the string end, not a further boundary. A bottom window must still
    render that final line."""
    sink = _StubSink(blocks=[])
    pane = ChatPane(app=_StubApp(sink))

    # Seed a pre-rendered full transcript + its bounds (as a dirty frame would
    # have produced), then force a bottom window by making the visible height
    # smaller than the transcript.
    text = "a\nb\nc\nd"  # no trailing newline: 4 lines, last is "d"
    pane._full_ansi_text = text
    pane._full_ansi_bounds = _line_boundaries(text)  # [0, 2, 4, 6]
    pane._total_chat_lines = 4
    pane._chat_content_epoch += 1  # invalidate the slice cache
    pane._auto_scroll = True  # stick to the tail
    sink.dirty = False  # skip the re-render; exercise only the slice

    with patch("shutil.get_terminal_size") as term:
        term.return_value = os.terminal_size((120, 6))  # height = 1
        pane._last_width = 116  # == max(40, 120 - 4): skip the re-render
        rendered = pane.render_chat()

    # A height-1 bottom window shows only the final line "d" — the exact
    # F-40 failure mode is rendering the wrong/empty final line.
    assert _visible(rendered.value) == ["d"]


def test_render_window_whole_short_transcript() -> None:
    """A transcript shorter than the window renders in full (top clamped to 0)."""
    sink = _StubSink(blocks=[])
    pane = ChatPane(app=_StubApp(sink))
    text = "only"
    pane._full_ansi_text = text
    pane._full_ansi_bounds = _line_boundaries(text)  # [0]
    pane._total_chat_lines = 1
    pane._chat_content_epoch += 1
    sink.dirty = False

    with patch("shutil.get_terminal_size") as term:
        term.return_value = os.terminal_size((120, 30))  # height = 25
        pane._last_width = 116
        rendered = pane.render_chat()

    assert _visible(rendered.value) == ["only"]


# ── Effective scroll / scrollbar plumbing ─────────────────────────────────────


def test_effective_scroll_and_scrollbar_state() -> None:
    """Auto-scroll pins to the tail; manual scroll clamps to [0, max]."""
    sink = _StubSink(blocks=[])
    pane = ChatPane(app=_StubApp(sink))
    with patch("shutil.get_terminal_size") as term:
        term.return_value = os.terminal_size((120, 30))  # height = 25

        pane._total_chat_lines = 100
        # Auto-scroll: the tail, i.e. max_scroll.
        pane._auto_scroll = True
        assert pane.get_effective_scroll() == 100 - 25
        assert pane.scrollbar_state() == (100, 100 - 25)

        # Manual scroll: clamped to the requested position.
        pane._auto_scroll = False
        pane._chat_scroll_top = 10
        assert pane.get_effective_scroll() == 10
        # Clamped at both ends.
        pane._chat_scroll_top = 0
        assert pane.get_effective_scroll() == 0
        pane._chat_scroll_top = 999
        assert pane.get_effective_scroll() == 100 - 25


def test_block_ansi_cache_generation_is_monotonic() -> None:
    """Underpins F-41: each ``clear`` advances the generation the fingerprint
    relies on, so repeated theme changes never hit a stale bounds cache."""
    cache = BlockAnsiCache()
    gen0 = cache.generation
    cache.clear(80)
    cache.clear(0)
    assert cache.generation == gen0 + 2
