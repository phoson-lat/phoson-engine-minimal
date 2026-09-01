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
"""

import re
from pathlib import Path
from urllib.parse import quote

#: OSC 8 open (``ESC ] 8 ; params ; URI ST``) or close (``ESC ] 8 ; ; ST``)
#: sequence. ``ST`` (string terminator) is matched as the canonical
#: ``ESC \`` form Rich emits (see ``rich.style.Style.render``) — Rich never
#: uses the BEL (``\\a``) legacy terminator, so that form isn't handled here.
_OSC8_RE = re.compile(r"\x1b\]8;[^\x1b]*\x1b\\")


def osc8_passthrough(ansi_text: str) -> str:
    """Wrap every OSC 8 hyperlink sequence in ``ansi_text`` for ``ANSI()``.

    Call this on Rich-rendered ANSI text *before* wrapping it in
    ``prompt_toolkit.formatted_text.ANSI(...)`` — it wraps each OSC 8
    open/close sequence in ``\\001``/``\\002`` so ``ANSI()`` treats it as a
    zero-width escape and prompt_toolkit's renderer writes it to the
    terminal raw and unmangled, instead of tearing it apart into visible
    text. A no-op on text with no hyperlinks.
    """
    return _OSC8_RE.sub(lambda m: "\x01" + m.group(0) + "\x02", ansi_text)


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


__all__ = ["osc8_passthrough"]
