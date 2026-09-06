"""Tests for T-15 (#172): Rich renderables → prompt_toolkit FormattedText.

Covers:

* ``formatting.renderable_to_formatted_text`` — the bridge from Rich
  renderables to ptk ``FormattedText`` (style preserved, non-empty output).
* ``render.BlockFormattedTextCache`` — the FormattedText counterpart of
  ``BlockAnsiCache``: same block + width returns the *same* cached object,
  a different width re-renders (different object).
* the app wiring: ``PhosonApp`` creates the cache in ``__init__`` and the
  cache is functional (see the ``TODO(T-15)`` in ``_render_chat`` for the
  intended fragment windowing that is not wired yet).
"""

import os
import shutil
from unittest.mock import MagicMock, patch

from rich.text import Text
from prompt_toolkit.formatted_text import FormattedText

from phoson_cli.config import PhosonConfig
from phoson_cli.fullscreen.render import (
    BlockFormattedTextCache,
    renderable_to_formatted_text,
)

# ── renderable_to_formatted_text ───────────────────────────────────────────────


def test_renderable_to_formatted_text_returns_formatted_text() -> None:
    ft = renderable_to_formatted_text(Text("hello", style="bold red"), 80)
    assert isinstance(ft, FormattedText)
    assert len(ft) > 0
    # Visible text is preserved.
    assert "".join(text for _, text in ft) == "hello"


def test_renderable_to_formatted_text_preserves_style() -> None:
    ft = renderable_to_formatted_text(Text("hello", style="bold red"), 80)
    # Every fragment carries a non-empty style.
    assert all(style for style, _ in ft)
    # Rich's "bold red" is encoded by ptk's ANSI parser as the named style
    # "ansired" + the "bold" attribute — the terminal-level equivalent of
    # SGR 1;31 (bold + red foreground).
    styles = {style for style, _ in ft}
    assert all("ansired" in s for s in styles), styles
    assert all("bold" in s for s in styles), styles
    # Visible text is preserved.
    assert "".join(text for _, text in ft) == "hello"


# ── BlockFormattedTextCache ────────────────────────────────────────────────────


def test_block_ft_cache_hit() -> None:
    cache = BlockFormattedTextCache()
    block = Text("cached block", style="bold")
    first = cache.get_or_render(block, 80)
    second = cache.get_or_render(block, 80)
    assert isinstance(first, FormattedText)
    assert first is second  # same object: the cache hit
    assert id(first) == id(second)


def test_block_ft_cache_width_invalidation() -> None:
    cache = BlockFormattedTextCache()
    block = Text("width sensitive", style="bold")
    at_80 = cache.get_or_render(block, 80)
    at_40 = cache.get_or_render(block, 40)
    assert at_80 is not at_40  # different width → re-render
    assert id(at_80) != id(at_40)


# ── app wiring (partial — see TODO(T-15) in _render_chat) ──────────────────────


def test_app_creates_block_ft_cache(tmp_path) -> None:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        from phoson_cli.fullscreen.app import PhosonApp

        app = PhosonApp(config)

    assert isinstance(app._block_ft_cache, BlockFormattedTextCache)
    # The constructor applies a theme (apply_theme → clear), so the
    # generation has moved on at least once; the cache is live either way.
    generation = app._block_ft_cache.generation
    assert generation >= 1

    # The cache is functional against the app's own sink blocks.
    from phoson_llm.schemas import Message

    shutil.get_terminal_size = lambda fallback=(80, 24): os.terminal_size((120, 30))
    app.sink.on_user_message("hi there", Message(role="user", content="hi there"))
    width = max(40, 120 - 4)
    block = app.sink.blocks[0]
    ft = app._block_ft_cache.get_or_render(block, width)
    assert isinstance(ft, FormattedText)
    assert "hi there" in "".join(text for _, text in ft)
    # A second call at the same width is a cache hit (same object).
    assert app._block_ft_cache.get_or_render(block, width) is ft
