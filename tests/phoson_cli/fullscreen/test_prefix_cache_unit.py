"""Frozen-prefix cache for the streaming chat pane.

While a turn streams, every token / spinner tick is a dirty frame. The joined
ANSI of the frozen transcript (all finished blocks) is cached on the
``BlockAnsiCache`` keyed by the sink's ``BlockList`` version counters, and the
chat pane keeps prefix and in-flight tail as separate strings, so a streaming
frame neither walks the block list nor concatenates the whole transcript.

These tests pin:

* the ``BlockList`` change counters (append vs. rewrite);
* that streaming frames do not re-join the prefix (no per-block lookups, the
  same prefix string object, cached prefix bounds);
* that appends only render the new blocks;
* correctness: after appends, live-turn edits, in-place block edits, removals,
  clear/reset, theme changes and resizes the pane's text, bounds and visible
  window equal an uncached render of the same sink;
* ``windowed_slice_parts`` == ``windowed_slice`` over the concatenation.
"""

import os
import random
from unittest.mock import MagicMock, patch

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_cli.fullscreen.sink import BlockList
from phoson_cli.fullscreen.render import (
    BlockAnsiCache,
    render_chat,
    windowed_slice,
    _line_boundaries,
    windowed_slice_parts,
)


def _app_for(tmp_path):
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        from phoson_cli.fullscreen.app import PhosonApp

        return PhosonApp(config)


def _term(columns: int = 120, lines: int = 30):
    return patch(
        "shutil.get_terminal_size",
        return_value=os.terminal_size((columns, lines)),
    )


def _push_transcript(app, n: int) -> None:
    msgs = [
        Message(role=("user" if i % 2 == 0 else "assistant"), content=f"msg {i}")
        for i in range(n)
    ]
    app.sink.on_user_message("msg 0", msgs[0])
    app.sink.print_history(msgs)


class _CountingCache(BlockAnsiCache):
    """A ``BlockAnsiCache`` that counts per-block lookups."""

    __slots__ = ("lookups",)

    def __init__(self) -> None:
        super().__init__()
        self.lookups = 0

    def get_or_render(self, block, width):
        self.lookups += 1
        return super().get_or_render(block, width)


# ── BlockList ─────────────────────────────────────────────────────────────────


def test_block_list_counters_distinguish_append_from_rewrite() -> None:
    blocks = BlockList(["a"])
    v0, r0 = blocks.version, blocks.rewrite_version

    blocks.append("b")
    blocks.extend(["c"])
    blocks += ["d"]
    assert blocks.version != v0
    assert blocks.rewrite_version == r0  # appends keep the old prefix intact

    for mutate in (
        lambda b: b.__setitem__(0, "z"),
        lambda b: b.__delitem__(0),
        lambda b: b.insert(0, "y"),
        lambda b: b.pop(),
        lambda b: b.remove("y"),
        lambda b: b.reverse(),
        lambda b: b.sort(),
        lambda b: b.__setitem__(slice(0, 1), ["q", "r"]),
        lambda b: b.clear(),
    ):
        before_v, before_r = blocks.version, blocks.rewrite_version
        mutate(blocks)
        assert blocks.version != before_v
        assert blocks.rewrite_version != before_r


def test_block_list_versions_are_never_reused_across_lists() -> None:
    a = BlockList(["x"])
    b = BlockList(["x"])
    assert a.version != b.version


def test_sink_wraps_reassigned_blocks(tmp_path) -> None:
    app = _app_for(tmp_path)
    assert isinstance(app.sink.blocks, BlockList)
    app.sink.blocks = ["one", "two"]
    assert isinstance(app.sink.blocks, BlockList)
    assert app.sink.blocks == ["one", "two"]


# ── render_prefix ─────────────────────────────────────────────────────────────


def test_render_prefix_reuses_join_and_renders_only_appended_blocks() -> None:
    cache = _CountingCache()
    blocks = BlockList([f"block {i}" for i in range(200)])
    first = cache.render_prefix(blocks, 80)
    assert cache.lookups == 200

    # Unchanged list: the very same string, no per-block work at all.
    for _ in range(50):
        assert cache.render_prefix(blocks, 80) is first
    assert cache.lookups == 200

    # Append: only the new block is looked up; the result extends the old.
    blocks.append("block new")
    extended = cache.render_prefix(blocks, 80)
    assert cache.lookups == 201
    assert extended.startswith(first)
    assert extended == "".join(BlockAnsiCache().get_or_render(b, 80) for b in blocks)

    # In-place edit: full re-join (from the per-block cache), correct output.
    blocks[3] = "edited"
    edited = cache.render_prefix(blocks, 80)
    assert cache.lookups == 201 + len(blocks)
    assert edited == "".join(BlockAnsiCache().get_or_render(b, 80) for b in blocks)

    # clear() (theme change / reset) drops the joined prefix as well.
    cache.clear(80)
    assert cache.render_prefix(blocks, 80) == edited
    assert cache.lookups == 201 + 2 * len(blocks)


def test_render_prefix_plain_list_is_uncached() -> None:
    cache = _CountingCache()
    blocks = ["a", "b"]
    cache.render_prefix(blocks, 80)
    cache.render_prefix(blocks, 80)
    assert cache.lookups == 4


# ── Streaming frames through the pane ─────────────────────────────────────────


def test_streaming_frames_do_not_rejoin_the_prefix(tmp_path) -> None:
    app = _app_for(tmp_path)
    counting = _CountingCache()
    app._block_ansi_cache = counting
    _push_transcript(app, 300)
    with _term():
        app.sink.begin_activity()
        app.sink.current_turn.content = "tok"
        app._render_chat()
        lookups_after_first = counting.lookups
        assert lookups_after_first >= 300
        prefix = app._chat_pane._win_prefix
        prefix_bounds = app._frozen_ansi_bounds

        for i in range(100):
            app.sink.current_turn.content += f" tok{i}" + ("\n" if i % 7 == 0 else "")
            app.sink.dirty = True
            app._render_chat()
            # Same frozen prefix object and bounds; zero per-block lookups.
            assert app._chat_pane._win_prefix is prefix
            assert app._frozen_ansi_bounds is prefix_bounds
        assert counting.lookups == lookups_after_first

        # The result is still exactly the uncached render.
        assert app._full_ansi_text == render_chat(app.sink, 116)
        assert app._full_ansi_bounds == _line_boundaries(app._full_ansi_text)


# ── Correctness against the uncached render ───────────────────────────────────


def _assert_matches_uncached(app, width: int) -> None:
    pane = app._chat_pane
    expected = render_chat(app.sink, width)
    assert app._full_ansi_text == expected
    bounds = _line_boundaries(expected)
    assert app._full_ansi_bounds == bounds
    assert app._total_chat_lines == max(1, len(bounds))
    for top in (0, 1, len(bounds) // 2, max(0, len(bounds) - 5)):
        for height in (1, 5, 25):
            assert windowed_slice_parts(
                pane._win_prefix,
                pane._win_prefix_bounds,
                pane._win_tail,
                pane._win_tail_bounds,
                top,
                height,
            ) == windowed_slice(expected, bounds, top, height)
    # The window the pane actually handed to prompt_toolkit.
    height = pane.get_visible_window_height()
    top = max(0, len(bounds) - height) if len(bounds) >= height else 0
    assert pane._windowed_ansi.value == windowed_slice(expected, bounds, top, height)


def test_pane_output_equals_uncached_render_across_mutations(tmp_path) -> None:
    from phoson_cli.theme import LIGHT

    app = _app_for(tmp_path)
    # The welcome banner animates on wall-clock time, so two renders of it
    # differ; use the static one-line empty-state hint instead.
    app.sink.welcome_provider = None
    with _term(120, 30):
        width = 116
        app._render_chat()
        _assert_matches_uncached(app, width)  # empty state

        _push_transcript(app, 40)
        app._render_chat()
        _assert_matches_uncached(app, width)

        # Live turn: grow, rewrite (shrink), multi-line.
        app.sink.begin_activity()
        for content in ("a", "abc\n", "abc\ndef", "x", "one\ntwo\nthree\n"):
            app.sink.current_turn.content = content
            app.sink.dirty = True
            app._render_chat()
            _assert_matches_uncached(app, width)

        # Append while streaming (frozen prefix grows).
        app.sink.blocks.append("appended block")
        app.sink.dirty = True
        app._render_chat()
        _assert_matches_uncached(app, width)

        # In-place edit of an earlier block, then a removal.
        app.sink.blocks[2] = "edited block"
        app.sink.dirty = True
        app._render_chat()
        _assert_matches_uncached(app, width)
        del app.sink.blocks[0]
        app.sink.dirty = True
        app._render_chat()
        _assert_matches_uncached(app, width)

        # Reassignment of the whole list (e.g. /details folding).
        app.sink.blocks = list(app.sink.blocks)[:10]
        app.sink.dirty = True
        app._render_chat()
        _assert_matches_uncached(app, width)

        # Theme change clears the block cache (F-41 path).
        app.apply_theme(LIGHT)
        app._render_chat()
        _assert_matches_uncached(app, width)

    # Resize: a different width re-renders everything.
    with _term(90, 20):
        app._render_chat()
        _assert_matches_uncached(app, 86)

    with _term(90, 20):
        # Clear (Ctrl+L style) while the turn is still streaming, then reset.
        app.sink.blocks.clear()
        app.sink.dirty = True
        app._render_chat()
        _assert_matches_uncached(app, 86)
        app.sink.reset_session_view()
        app._render_chat()
        _assert_matches_uncached(app, 86)
        _push_transcript(app, 6)
        app._render_chat()
        _assert_matches_uncached(app, 86)


# ── windowed_slice_parts ──────────────────────────────────────────────────────


def test_windowed_slice_parts_equals_windowed_slice_random() -> None:
    rng = random.Random(1234)
    alphabet = ["a", "b", "\n", "\x1b[1m", "\x1b[0m"]
    for _ in range(400):
        prefix = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        tail = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        full = prefix + tail
        full_bounds = _line_boundaries(full)
        pb, tb = _line_boundaries(prefix), _line_boundaries(tail)
        total = len(full_bounds)
        assert len(pb) + len(tb) - 1 == total
        for top in range(-1, total + 2):
            for height in (0, 1, 2, 3, total + 1):
                assert windowed_slice_parts(
                    prefix, pb, tail, tb, top, height
                ) == windowed_slice(full, full_bounds, top, height)
