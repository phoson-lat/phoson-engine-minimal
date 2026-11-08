"""Make Rich-rendered text safe for prompt_toolkit's screen model.

Rich lays out each line with its own cell widths: Rich 15 measures whole
grapheme clusters (``⚠️`` is 2 cells, a ZWJ family ``👨‍👩‍👧`` is 2). The full-
screen TUI then hands that text to prompt_toolkit, which places every
*code point* with ``wcwidth`` (``⚠️`` → 1, the family → 6). When the two
disagree, prompt_toolkit's idea of where each character sits drifts from
the line Rich built, and from what the terminal draws: text shifts, wraps
early or overwrites itself — the "characters break the text" bug.

Raw control bytes do the same: an OSC title sequence or a bell in tool
output is laid out by Rich as visible cells and then partially swallowed by
prompt_toolkit's ANSI parser, leaving fragments such as ``0;title``.

:func:`safe_segment_text` rewrites one already-laid-out run of text so that
its prompt_toolkit width equals its Rich width, cluster by cluster:

- escape sequences (CSI / OSC / other ``ESC`` forms) and C0 control
  characters are dropped, and the cells Rich gave them become spaces;
- a multi-code-point cluster whose widths disagree (emoji with a variation
  selector, ZWJ sequences, skin-tone modifiers, flags, keycaps) is reduced
  to its base character, padded with spaces to Rich's width — or blanked
  when even the base does not fit.

Because widths are preserved exactly, Rich's wrapping and padding stay
valid, and what is drawn uses only characters whose width terminals and
``wcwidth`` agree on.
"""

import re

from rich.cells import cell_len, split_graphemes
from prompt_toolkit.utils import get_cwidth

#: Escape sequences: OSC (``ESC ] … BEL|ST``), CSI (``ESC [ … final``) and
#: two-byte ``ESC x`` forms. Rich drops the BEL that usually ends an OSC, so
#: an unterminated OSC stops at the first whitespace instead of eating the
#: rest of the line.
_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\][^\x07\x1b\s]*"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-_]?"
)

#: Code points that only modify a neighbour (dropped when reducing a cluster).
_MODIFIERS = frozenset([0xFE0E, 0xFE0F, 0x200D, 0x20E3] + list(range(0x1F3FB, 0x1F400)))


def _ptk_width(text: str) -> int:
    return sum(get_cwidth(char) for char in text)


def _is_control(char: str) -> bool:
    code = ord(char)
    return (code < 0x20 and char != "\n") or code == 0x7F


def _needs_work(text: str) -> bool:
    return not (text.isascii() and (text.isprintable() or text == "\n"))


def _blank_escapes(text: str) -> str:
    """Replace escape sequences with spaces occupying the same Rich width."""
    if "\x1b" not in text:
        return text
    return _ESCAPE_RE.sub(lambda m: " " * cell_len(m.group(0)), text)


_REGIONAL_A = 0x1F1E6


def _safe_cluster(cluster: str, width: int) -> str:
    cleaned = "".join(c for c in cluster if not _is_control(c))
    if _ptk_width(cleaned) == width:
        return cleaned
    if cleaned and all(_REGIONAL_A <= ord(c) <= _REGIONAL_A + 25 for c in cleaned):
        # A flag (regional-indicator pair) becomes its country letters: 🇲🇽 → MX.
        letters = "".join(chr(ord("A") + ord(c) - _REGIONAL_A) for c in cleaned)
        letters = letters[:width]
        return letters + " " * (width - len(letters))
    base = next(
        (c for c in cleaned if ord(c) not in _MODIFIERS and get_cwidth(c) > 0), ""
    )
    base_width = _ptk_width(base)
    if base and base_width <= width:
        return base + " " * (width - base_width)
    return " " * width


def safe_segment_text(text: str) -> str:
    """Return ``text`` rewritten so prompt_toolkit's width equals Rich's."""
    if not _needs_work(text):
        return text
    text = _blank_escapes(text)
    spans, _total = split_graphemes(text)
    out: list[str] = []
    for start, end, width in spans:
        cluster = text[start:end]
        if cluster == "\n" or (len(cluster) == 1 and _ptk_width(cluster) == width):
            if not _is_control(cluster):
                out.append(cluster)
                continue
        out.append(_safe_cluster(cluster, width))
    return "".join(out)


__all__ = ["safe_segment_text"]
