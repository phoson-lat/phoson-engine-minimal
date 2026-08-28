"""Unit tests for phoson_cli.hyperlinks (IMPROVEMENTS.md G4, issue #58).

Covers the pure OSC 8 passthrough helper in isolation, plus an end-to-end
check that the wrapped sequence really does survive
``prompt_toolkit.formatted_text.ANSI()`` (the whole point of G4) and that
the full-screen render path (``fullscreen/render.py``) applies it.
"""

import io

from rich.console import Console
from rich.markdown import Markdown
from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from phoson_cli.theme import DARK
from phoson_cli.hyperlinks import osc8_passthrough
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.fullscreen.render import BlockAnsiCache, render_chat


def _rich_osc8(markup: str, width: int = 100) -> str:
    """Render *markup* through a real Rich Markdown with hyperlinks=True."""
    buf = io.StringIO()
    console = Console(
        file=buf, force_terminal=True, color_system="truecolor", width=width
    )
    console.print(Markdown(markup, hyperlinks=True))
    return buf.getvalue()


def test_osc8_passthrough_wraps_open_and_close_sequences() -> None:
    raw = _rich_osc8("[our site](https://phoson.lat)")
    assert "\x1b]8;" in raw  # sanity: Rich did emit OSC 8

    wrapped = osc8_passthrough(raw)

    # Every OSC 8 escape is now preceded by SOH and followed by STX.
    assert "\x01\x1b]8;" in wrapped
    assert "\x1b\\\x02" in wrapped
    # Nothing else in the string changed (same length delta as the two
    # markers added per OSC 8 sequence — open + close = 2 sequences here).
    assert wrapped.count("\x01") == wrapped.count("\x02") == 2


def test_osc8_passthrough_is_a_noop_on_plain_ansi() -> None:
    plain = "\x1b[94mhello\x1b[0m world\n"
    assert osc8_passthrough(plain) == plain


def test_osc8_passthrough_is_a_noop_on_plain_text() -> None:
    assert osc8_passthrough("no escapes here") == "no escapes here"


def test_osc8_passthrough_handles_multiple_links() -> None:
    raw = _rich_osc8("[a](https://a.com) and [b](https://b.com)")
    wrapped = osc8_passthrough(raw)

    # 2 links => 2 open + 2 close = 4 OSC 8 sequences, each wrapped once.
    assert wrapped.count("\x01\x1b]8;") == 4
    assert wrapped.count("\x1b\\\x02") == 4


def test_wrapped_osc8_survives_prompt_toolkit_ansi_parse() -> None:
    """The actual point of G4: after wrapping, ANSI() must hand the OSC 8

    bytes through as a single raw ``[ZeroWidthEscape]`` fragment rather
    than tearing them into visible-text fragments — the original bug
    (raw escape bytes leaking as literal text around the link).
    """
    raw = _rich_osc8("[our site](https://phoson.lat)")
    wrapped = osc8_passthrough(raw)

    fragments = to_formatted_text(ANSI(wrapped))
    styles = [style for style, _text, *_ in fragments]
    texts = [text for _style, text, *_ in fragments]

    assert any("[ZeroWidthEscape]" in style for style in styles)
    # The raw OSC 8 bytes never show up as part of a *visible* (non
    # zero-width) fragment's text — they only ever appear inside the
    # ZeroWidthEscape fragments themselves.
    visible_text = "".join(
        text for style, text in zip(styles, texts) if "[ZeroWidthEscape]" not in style
    )
    assert "\x1b]8;" not in visible_text
    assert "8;id=" not in visible_text
    # And the link text itself is still there, readable.
    assert "our site" in visible_text


def test_unwrapped_osc8_is_torn_apart_by_ansi_without_the_fix() -> None:
    """Control case: without osc8_passthrough, ANSI() does NOT treat OSC 8

    as a single unit — its raw bytes leak into a visible-text fragment.
    This is the bug G4's fix (osc8_passthrough) exists to prevent.
    """
    raw = _rich_osc8("[our site](https://phoson.lat)")

    fragments = to_formatted_text(ANSI(raw))
    styles = [style for style, _text, *_ in fragments]
    texts = [text for _style, text, *_ in fragments]

    visible_text = "".join(
        text for style, text in zip(styles, texts) if "[ZeroWidthEscape]" not in style
    )
    # Without the fix, the OSC 8 params leak as literal visible text.
    assert "id=" in visible_text or "phoson.lat" in visible_text


def test_render_chat_applies_osc8_passthrough_to_cached_blocks() -> None:
    """The full-screen bridge (BlockAnsiCache.get_or_render) must apply

    osc8_passthrough to every transcript block before caching it, so a
    frozen assistant turn with a Markdown link survives the app's own
    ANSI() re-parse (see PhosonApp._render_chat).
    """
    sink = FullScreenSink(on_invalidate=lambda: None, theme=DARK, show_reasoning=True)
    sink.blocks.append(
        Markdown(
            "Visit [our site](https://phoson.lat) for more.",
            style=DARK.text,
            hyperlinks=True,
        )
    )
    sink.dirty = True

    cache = BlockAnsiCache()
    text = render_chat(sink, width=80, cache=cache)

    assert "\x01\x1b]8;" in text
    assert "\x1b\\\x02" in text

    # And it survives the same ANSI() wrap the app itself performs.
    fragments = to_formatted_text(ANSI(text))
    visible_text = "".join(t for s, t, *_ in fragments if "[ZeroWidthEscape]" not in s)
    assert "\x1b]8;" not in visible_text
    assert "our site" in visible_text
