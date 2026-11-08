"""OSC 8 hyperlink passthrough for the full-screen Rich→ANSI bridge.

(IMPROVEMENTS.md G4, issue #58)

Rich's ``Markdown`` renders links as real terminal hyperlinks by default —
an OSC 8 escape sequence (``ESC ] 8 ; params ; URI ESC \\``) wrapping the
link text, exactly what ``printf '\\e]8;;http://example.com\\e\\\\text\\e]8;;\\e\\\\'``
produces. Terminals that support it (kitty, WezTerm, iTerm2, GNOME
Terminal/VTE, Ghostty, …) make that text clickable and open the URI —
without any color/attribute change, purely an additional per-cell hyperlink
attribute (see the OSC 8 spec:
https://gist.github.com/egmontkob/eb114294efbcd5adb1944c9f3cb5feda).

The classic REPL prints straight to a real terminal (``Renderer.console``
is a plain ``rich.console.Console``), so that OSC 8 sequence reaches the
terminal untouched — no fix needed there once ``hyperlinks=True``.

The full-screen TUI is the one that breaks it: the chat pane's content is
first rendered into a throwaway ``rich.console.Console`` (to capture ANSI
text — see :mod:`phoson_cli.fullscreen.render`) and then re-parsed by
``prompt_toolkit.formatted_text.ANSI()`` before prompt_toolkit's own VT100
output writer re-emits it. ``ANSI()``'s parser only understands CSI/SGR
escapes (``ESC [ ... m``) — anything starting with ``ESC ]`` (OSC) that
isn't followed by ``[`` falls through its "continue" branch character by
character, so an OSC 8 sequence gets torn apart into literal text (the
raw bytes ``8;id=...;https://...`` show up around the link — the bug G4
was originally filed for, and the reason ``hyperlinks=False`` was added).

The fix is the mechanism prompt_toolkit itself documents for exactly this
case: text between ``\\001`` (SOH) and ``\\002`` (STX) is parsed by
``ANSI()`` into a ``"[ZeroWidthEscape]"`` fragment, and prompt_toolkit's
renderer writes any fragment whose style contains that marker with
``output.write_raw()`` — bypassing width accounting and escaping, i.e.
exactly "pass these bytes to the terminal untouched". Wrapping each OSC 8
sequence in ``\\001...\\002`` before handing the string to ``ANSI()`` is
enough to carry it through intact; verified end-to-end against a real
``Vt100_Output`` writer (the OSC 8 bytes survive character-for-character).

There is a second, subtler failure that has nothing to do with ``ANSI()``:
a close sitting on Rich's trailing line padding is never *written*.
prompt_toolkit emits a zero-width escape only for the column it is about
to repaint, and ``_output_screen_diff`` stops at ``get_max_column_index``,
which ignores unstyled trailing whitespace — so the close's column is
skipped while the open (on the rendered link text) is written. The terminal
is left holding an open hyperlink and renders it as an underline on every
following cell (prompt_toolkit #1651). :func:`osc8_passthrough` anchors
such a close to a rendered cell before wrapping; see
:func:`osc8_passthrough` and :data:`_CLOSE_ON_TRAILING_WS_RE`.
"""

import re
from pathlib import Path
from urllib.parse import quote

#: OSC 8 open (``ESC ] 8 ; params ; URI ST``) or close (``ESC ] 8 ; ; ST``)
#: sequence. ``ST`` (string terminator) is matched as the canonical
#: ``ESC \`` form Rich emits (see ``rich.style.Style.render``) — Rich never
#: uses the BEL (``\\a``) legacy terminator, so that form isn't handled here.
_OSC8_RE = re.compile(r"\x1b\]8;[^\x1b]*\x1b\\")

#: Just the OSC 8 *close* form (``ESC ] 8 ; ; ST``), i.e. the sequence that
#: ends a hyperlink.
_OSC8_CLOSE_RE = re.compile(r"\x1b\]8;;\x1b\\")

#: An OSC 8 close immediately followed by a space that trails to the end of
#: the visual line (only more spaces, then a newline / end of string).
#:
#: Rich pads every rendered line to the console width with *unstyled*
#: spaces, and a Markdown link that ends a paragraph/line has its OSC 8
#: close sitting right before that padding. prompt_toolkit's
#: ``_output_screen_diff`` stops its per-row loop at
#: ``get_max_column_index``, which deliberately ignores unstyled trailing
#: whitespace — so the close's column is never visited and the close is
#: never written, while the *open* (on the rendered link text) is. The
#: terminal is then left holding an open hyperlink, which is rendered as an
#: underline on every cell that follows until a close happens to be
#: emitted: the "everything is underlined" bug. (prompt_toolkit issue
#: #1651 — "zero-width-escapes at the end of a line are not rendered".)
_CLOSE_ON_TRAILING_WS_RE = re.compile(
    r"(" + _OSC8_CLOSE_RE.pattern + r")[ \t](?=[ \t]*(?:\n|\Z))"
)


def osc8_passthrough(ansi_text: str) -> str:
    """Wrap every OSC 8 hyperlink sequence in ``ansi_text`` for ``ANSI()``.

    Call this on Rich-rendered ANSI text *before* wrapping it in
    ``prompt_toolkit.formatted_text.ANSI(...)`` — it wraps each OSC 8
    open/close sequence in ``\\001``/``\\002`` so ``ANSI()`` treats it as a
    zero-width escape and prompt_toolkit's renderer writes it to the
    terminal raw and unmangled, instead of tearing it apart into visible
    text. A no-op on text with no hyperlinks.

    Before wrapping, a close that would land on Rich's *trailing* padding
    is anchored to a rendered cell: the first padding space becomes a
    FIGURE SPACE (``\\u2007``, width 1, visually blank). That cell is not
    ``" "`` to ``get_max_column_index``, so prompt_toolkit still visits the
    column and emits the close — otherwise an open hyperlink is left
    dangling at end of line and underlines everything that follows (see
    :data:`_CLOSE_ON_TRAILING_WS_RE` and prompt_toolkit #1651). Replacing
    (rather than inserting) keeps the line at its padded width, so no
    wrapping/blanking is introduced.

    ``\\u00a0`` is deliberately *not* used: prompt_toolkit rewrites it to a
    plain space carrying ``class:nbsp`` ("will be underlined"), which counts
    as `" "` again and defeats the fix.
    """
    if "\x1b]8;" not in ansi_text:
        return ansi_text
    ansi_text = _CLOSE_ON_TRAILING_WS_RE.sub(lambda m: m.group(1) + "\u2007", ansi_text)
    return _OSC8_RE.sub(lambda m: "\x01" + m.group(0) + "\x02", ansi_text)


#: An OSC 8 sequence; group 1 is the URI (empty for a close).
_OSC8_PARTS_RE = re.compile(r"\x1b\]8;[^;\x1b]*;([^\x1b]*)\x1b\\")

#: The OSC 8 close sequence written to end an open hyperlink.
OSC8_CLOSE = "\x1b]8;;\x1b\\"

#: Output methods that move the cursor or end a write burst. A hyperlink
#: must never stay open across them (see :func:`guard_hyperlinks`).
_BOUNDARY_METHODS = (
    "cursor_goto",
    "cursor_up",
    "cursor_down",
    "cursor_forward",
    "cursor_backward",
    "erase_screen",
    "erase_end_of_line",
    "erase_down",
    "enter_alternate_screen",
    "quit_alternate_screen",
    "flush",
)


def guard_hyperlinks(output: object) -> object:
    """Keep a prompt_toolkit ``Output`` from leaving a hyperlink open.

    prompt_toolkit repaints only the cells that changed. An OSC 8 *open*
    rides on the first cell of a link and the *close* on the cell after it,
    so a repaint that rewrites the first cell but not the last (scrolling,
    a streamed line, a resize) writes the open without the close. Terminals
    treat the link as "on" for every character written afterwards — the
    footer, the spinner, the next lines — so all of it turns into one big
    hyperlink: the "everything is highlighted as a link" bug.

    This wraps ``output`` in place: it tracks whether the last OSC 8
    sequence written through ``write_raw`` opened a link, and writes a close
    before any cursor movement, erase, line break or flush while one is
    open. A link's own cells are written contiguously, so they keep their
    hyperlink; nothing outside them can inherit it. Returns ``output``.
    """
    write_raw = getattr(output, "write_raw", None)
    if write_raw is None or getattr(output, "_phoson_link_guard", False):
        return output
    state = {"open": False}

    def _close_if_open() -> None:
        if state["open"]:
            state["open"] = False
            write_raw(OSC8_CLOSE)

    def guarded_write_raw(data: str) -> None:
        last = None
        for last in _OSC8_PARTS_RE.finditer(data):
            pass
        if last is not None:
            state["open"] = bool(last.group(1))
        write_raw(data)

    write = getattr(output, "write", None)
    if write is not None:

        def guarded_write(data: str) -> None:
            if "\n" in data or "\r" in data:
                _close_if_open()
            write(data)

        output.write = guarded_write  # type: ignore[attr-defined]

    for name in _BOUNDARY_METHODS:
        method = getattr(output, name, None)
        if method is None:
            continue

        def guarded(*args, _method=method, **kwargs):  # noqa: ANN002, ANN003
            _close_if_open()
            return _method(*args, **kwargs)

        setattr(output, name, guarded)
    output.write_raw = guarded_write_raw  # type: ignore[attr-defined]
    output._phoson_link_guard = True  # type: ignore[attr-defined]
    return output


def file_uri(path: str) -> str:
    """OSC 8 ``file://`` URI for a path (T-7 tool-card links).

    Relative paths are resolved against ``Path.cwd()`` first — a
    ``file://src/app.py`` that doesn't exist is a dead link. Spaces and
    other non-ASCII-safe characters are percent-encoded.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    return "file://" + quote(str(resolved), safe="/")


__all__ = ["OSC8_CLOSE", "guard_hyperlinks", "osc8_passthrough"]
