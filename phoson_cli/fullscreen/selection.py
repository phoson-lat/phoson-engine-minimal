"""Drag-to-select for the chat pane: the range, its text and its highlight.

Pure functions over *rendered* chat lines — the pane renders Rich output to
ANSI lines, so a selection is a pair of ``(line, cell)`` points in that
rendered grid (``line`` indexes the whole transcript, ``cell`` is a terminal
column). Nothing here touches the per-block render cache: the copied text is
read from the cached lines, and the highlight is an overlay built over the
visible window's fragments only.

Columns are terminal *cells*, measured with prompt_toolkit's ``get_cwidth``
— the same widths the pane lays out with (``terminal_text`` normalizes every
rendered segment to them), so a wide character or emoji is one unit two
cells wide.
"""

from dataclasses import dataclass
from collections.abc import Callable

from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.formatted_text import (
    ANSI,
    StyleAndTextTuples,
    to_formatted_text,
)
from prompt_toolkit.formatted_text.utils import split_lines

Point = tuple[int, int]  # (transcript line, cell)

_ZERO_WIDTH = "[ZeroWidthEscape]"
#: ``osc8_passthrough`` anchors a link's close on a figure space that stands
#: in for one of Rich's padding spaces; copy it back as the space it was.
_FIGURE_SPACE = " "


@dataclass(frozen=True)
class Selection:
    """A text range between two points, in the order they were picked."""

    anchor: Point
    cursor: Point

    def ordered(self) -> tuple[Point, Point]:
        """``(start, end)`` in reading order; both cells are included."""
        return (
            (self.anchor, self.cursor)
            if self.anchor <= self.cursor
            else (self.cursor, self.anchor)
        )

    def cells_on(self, line: int) -> tuple[int, int | None] | None:
        """Selected cells ``[start, stop)`` on *line* (``stop=None``: to the
        end of the line), or ``None`` when the line is outside the range."""
        (first, first_cell), (last, last_cell) = self.ordered()
        if not first <= line <= last:
            return None
        start = first_cell if line == first else 0
        stop = last_cell + 1 if line == last else None
        return start, stop


def plain_line(ansi_line: str) -> str:
    """The visible text of one rendered line: no SGR, no OSC 8, no newline."""
    fragments = to_formatted_text(ANSI(ansi_line))
    return _fragments_text(fragments).rstrip("\n")


def _fragments_text(fragments: StyleAndTextTuples) -> str:
    text = "".join(f[1] for f in fragments if _ZERO_WIDTH not in f[0])
    return text.replace(_FIGURE_SPACE, " ")


def cell_width(text: str) -> int:
    return sum(get_cwidth(char) for char in text)


def slice_cells(text: str, start: int, stop: int | None) -> str:
    """Characters of *text* that occupy any cell in ``[start, stop)``.

    A wide character is taken whole when the range covers either of its
    cells; a zero-width character follows the character it attaches to.
    """
    out: list[str] = []
    cell = 0
    taken = False
    for char in text:
        width = get_cwidth(char)
        if width > 0:
            taken = cell + width > start and (stop is None or cell < stop)
        if taken:
            out.append(char)
        cell += width
    return "".join(out)


def selected_text(get_line: Callable[[int], str], selection: Selection) -> str:
    """Plain text of *selection*; ``get_line(n)`` is rendered ANSI line *n*.

    Lines are joined with ``\\n`` and lose their trailing padding. Each
    rendered line is one copied line, so a soft-wrapped paragraph comes out
    as the lines it was wrapped into.
    """
    (first, _), (last, _) = selection.ordered()
    lines: list[str] = []
    for line in range(first, last + 1):
        cells = selection.cells_on(line)
        assert cells is not None
        lines.append(slice_cells(plain_line(get_line(line)), *cells).rstrip())
    return "\n".join(lines)


def word_cells(text: str, cell: int) -> tuple[int, int] | None:
    """First and last cell of the whitespace-delimited word under *cell*."""
    spans: list[tuple[int, int, str]] = []
    position = 0
    for char in text:
        width = get_cwidth(char)
        spans.append((position, position + max(width, 1) - 1, char))
        position += width
    index = next((i for i, (a, b, _) in enumerate(spans) if a <= cell <= b), None)
    if index is None or spans[index][2].isspace():
        return None
    low = high = index
    while low > 0 and not spans[low - 1][2].isspace():
        low -= 1
    while high + 1 < len(spans) and not spans[high + 1][2].isspace():
        high += 1
    return spans[low][0], spans[high][1]


def highlight_window(
    fragments: StyleAndTextTuples, top: int, selection: Selection
) -> StyleAndTextTuples:
    """*fragments* (the visible window, first line = transcript line *top*)
    with the selected cells in reverse video.

    Only cells that hold text are highlighted — the padding to the right of
    a line is not part of what gets copied.
    """
    out: StyleAndTextTuples = []
    for row, line in enumerate(split_lines(fragments)):
        if row:
            out.append(("", "\n"))
        cells = selection.cells_on(top + row)
        if cells is None:
            out.extend(line)
            continue
        start, stop = cells
        text_end = cell_width(_fragments_text(line).rstrip())
        stop = text_end if stop is None else min(stop, text_end)
        out.extend(_highlight_line(line, start, stop))
    return out


def _highlight_line(
    line: StyleAndTextTuples, start: int, stop: int
) -> StyleAndTextTuples:
    if stop <= start:
        return list(line)
    out: StyleAndTextTuples = []
    cell = 0
    for fragment in line:
        style, text = fragment[0], fragment[1]
        if _ZERO_WIDTH in style:
            out.append(fragment)
            continue
        run: list[str] = []
        run_selected = False
        selected = False
        for char in text:
            width = get_cwidth(char)
            if width > 0:
                selected = cell + width > start and cell < stop
            if run and selected != run_selected:
                out.append((_reversed(style) if run_selected else style, "".join(run)))
                run = []
            run_selected = selected
            run.append(char)
            cell += width
        if run:
            out.append((_reversed(style) if run_selected else style, "".join(run)))
    return out


def _reversed(style: str) -> str:
    # A cell already in reverse video flips back, so it still stands out.
    return f"{style} noreverse" if "reverse" in style.split() else f"{style} reverse"


__all__ = [
    "Selection",
    "highlight_window",
    "plain_line",
    "selected_text",
    "slice_cells",
    "word_cells",
]
