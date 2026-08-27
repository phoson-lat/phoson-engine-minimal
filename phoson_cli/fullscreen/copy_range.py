"""Pure helpers for the full-screen copy mode (IMPROVEMENTS.md G3, #57).

In full-screen, ``mouse_support=True`` (chat scroll-wheel) captures the
mouse and removes the terminal's native click-drag selection. G3's
terminal-independent answer is a keyboard-driven *copy mode*: the user
anchors a start point, extends a range with the arrows, and the range is
yanked to the system clipboard. The range math lives here so it can be
unit-tested without a running ``Application``.

Selection state is a pair of :class:`Pos` over a *linear* index space: each
rendered chat line is one unit, and a position's ``col`` is the character
offset within that line (0-based). The selected text always spans from the
lower position to the higher one (in ``(line, col)`` order), matching how
the user extends it in either direction.
"""

from typing import NamedTuple
from collections.abc import Sequence

#: Parsed ANSI fragments: ``(style, text)`` pairs, where the style is a
#: prompt_toolkit style string (or ``""`` for the default). ``app.py``
#: normalizes ``to_formatted_text(ANSI(...))`` into exactly this shape at the
#: boundary (dropping any 3-tuple mouse-handler element it may carry), so the
#: pure helpers here stay free of prompt_toolkit types.
Fragments = Sequence[tuple[str, str]]


class Pos(NamedTuple):
    """A point in the rendered chat: ``line`` (0-based rendered row) and
    ``col`` (0-based character offset within that row)."""

    line: int
    col: int

    def linear(self) -> int:
        """Flatten into a single ascending index over the whole transcript.

        ``line`` dominates, ``col`` breaks ties — so comparing two
        positions with one integer is equivalent to comparing ``(line,
        col)`` pairs lexicographically.
        """
        return self.line * 1_000_000 + self.col


def clamp_position(lines: list[str], pos: Pos) -> Pos:
    """Clamp a position into the rendered transcript.

    ``line`` is clamped to ``[0, len(lines) - 1]`` and ``col`` to the
    length of that line. Out-of-range input (a position computed at a wider
    pane, or after the transcript shrank) is snapped to the nearest valid
    cell rather than raising — copy mode must never crash the app over a
    stale cursor.
    """
    if not lines:
        return Pos(0, 0)
    line = max(0, min(pos.line, len(lines) - 1))
    col = _clamp_col(lines[line], pos.col)
    return Pos(line, col)


def _clamp_col(line_text: str, col: int) -> int:
    """Clamp ``col`` into ``[0, len(line_text)]`` (a cursor may sit at or
    past the last visible character of a line)."""
    if col < 0:
        return 0
    return min(col, len(line_text))


def range_text(lines: list[str], start: Pos, end: Pos) -> str:
    """The text selected between ``start`` and ``end`` (either order).

    Selection semantics (the intuitive "anchor + drag" model):
    - A same-line selection from ``col 2`` to ``col 5`` yields exactly the
      characters at columns 2..4.
    - A multi-line selection grabs the rest of the start line, every full
      line between, and the prefix of the end line up to (not including)
      the end column. Blank lines in between are preserved.
    """
    if not lines:
        return ""
    a = clamp_position(lines, start)
    b = clamp_position(lines, end)
    if a.linear() > b.linear():
        a, b = b, a

    if a.line == b.line:
        return lines[a.line][a.col : b.col]

    pieces: list[str] = [lines[a.line][a.col :]]
    pieces.extend(lines[line] for line in range(a.line + 1, b.line))
    pieces.append(lines[b.line][: b.col])
    return "\n".join(pieces)


def selection_line_span(lines: list[str], start: Pos, end: Pos) -> tuple[int, int]:
    """Inclusive ``(first, last)`` rendered line indices covered by the
    range — used to highlight the selected rows in the pane."""
    if not lines:
        return (0, 0)
    a = clamp_position(lines, start)
    b = clamp_position(lines, end)
    lo, hi = (a, b) if a.linear() <= b.linear() else (b, a)
    return (lo.line, hi.line)


def step_page(lines: list[str], pos: Pos, step: int) -> Pos:
    """Move *pos* ``step`` whole pages (in lines) and land at a page edge.

    ``step`` is in *lines* (one visible page of the pane). The destination
    sits at the far edge of the target page — column 0 for a forward step,
    the last line's length for a backward step — so a page extension grabs
    a whole page of text. ``pos`` is returned unchanged (clamped) when
    *step* is 0 or the target is out of range.
    """
    if not lines:
        return Pos(0, 0)
    pos = clamp_position(lines, pos)
    target_line = pos.line + step
    if step == 0:
        return pos
    if target_line <= 0:
        return Pos(0, 0)
    if target_line >= len(lines):
        return Pos(len(lines) - 1, len(lines[-1]))
    if step > 0:
        return Pos(target_line, 0)
    return Pos(target_line, len(lines[target_line]))


def plain_lines(fragments: "Fragments") -> list[str]:
    """Flatten parsed ANSI ``fragments`` into plain text lines (no escapes).

    This is the coordinate space copy mode's range math and highlighting work
    in. Each ``\\n`` in a fragment ends the current line and starts a new one;
    a fragment's first segment *continues* the current line (so styled runs
    within one row merge). A trailing ``\\n`` therefore yields a final empty
    line — the empty row the cursor sits on after the last wrap. The line
    count is always (number of newlines) + 1.
    """
    lines: list[str] = []
    current = ""
    for _style, text in fragments:
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i == 0:
                current += part
            else:
                lines.append(current)
                current = part
    lines.append(current)
    return lines


def apply_reverse_highlight(fragments: "Fragments", lo: int, hi: int) -> str:
    """Re-emit ``fragments`` as an ANSI string with rows ``lo..hi`` reversed.

    Walks the fragments with the *same* line accounting as
    :func:`plain_lines` (a ``\\n`` ends a line; a fragment's first segment
    continues the current line) so the highlighted rows line up exactly with
    the indices the range math produces. Only non-empty rows inside the span
    are wrapped in a reverse-video marker (``\\x1b[7m`` … ``\\x1b[27m``); a
    blank row in the range is left blank (the rows above and below it carry
    the band). Returns a fresh ANSI string the caller wraps.
    """
    out: list[str] = []
    line_index = 0
    for _style, chunk in fragments:
        parts = chunk.split("\n")
        for i, part in enumerate(parts):
            if part and lo <= line_index <= hi:
                out.append("\x1b[7m" + part + "\x1b[27m")
            else:
                out.append(part)
            if i < len(parts) - 1:
                out.append("\n")
                line_index += 1
    return "".join(out)


__all__ = [
    "Pos",
    "clamp_position",
    "range_text",
    "selection_line_span",
    "step_page",
    "plain_lines",
    "apply_reverse_highlight",
]
