"""Animated phoson-cli banner for the full-screen welcome screen.

A compact (7-line) take on the phoson face from ``phos-ascii.txt`` beside a
pixel-font ``phoson-cli`` wordmark drawn in blocks. Two light
animations, both pure functions of the elapsed time ``t`` so the renderer
stays stateless:

- a *shine* — a soft highlight band sweeping across the wordmark, then a
  pause (:data:`SHINE_PERIOD`);
- a *blink* — the face's eyes close for a moment every :data:`BLINK_PERIOD`.

:func:`banner_frame_key` changes only when the picture does, so the app's
ticker repaints during a sweep or a blink and stays idle in between.
"""

import os
import math

from rich.text import Text
from rich.color import Color, ColorParseError
from rich.console import Console, RenderResult, ConsoleOptions

#: The phoson face, compact. ``{eyes}`` is replaced per frame (open/blink).
_LOGO: tuple[str, ...] = (
    "    ▄▄██████▄▄    ",
    "  ▄█▀        ▀█▄  ",
    " █▀  {eyes}  ▀█ ",
    " █              █ ",
    " █▄   ▀▄▄▄▄▀   ▄█ ",
    "  ▀█▄        ▄█▀  ",
    "    ▀▀██████▀▀    ",
)
_EYES_OPEN = "▓▓    ▓▓"
_EYES_SHUT = "▂▂    ▂▂"
LOGO_WIDTH = len(_LOGO[0])

#: 5-row glyphs, one cell per pixel ("#" = on). Round letters sit on a
#: 4-row x-height under the ascenders of p/h/l/i.
_GLYPHS: dict[str, tuple[str, ...]] = {
    "p": ("###.", "#..#", "###.", "#...", "#..."),
    "h": ("#...", "#...", "###.", "#..#", "#..#"),
    "o": ("....", ".##.", "#..#", "#..#", ".##."),
    "s": ("....", ".###", "##..", "..##", "###."),
    "n": ("....", "###.", "#..#", "#..#", "#..#"),
    "-": ("...", "...", "###", "...", "..."),
    "c": ("....", ".###", "#...", "#...", ".###"),
    "l": ("#..", "#..", "#..", "#..", ".##"),
    "i": ("#", ".", "#", "#", "#"),
}

#: Seconds per shine cycle, of which :data:`SHINE_SWEEP` is the sweep.
SHINE_PERIOD = 5.0
SHINE_SWEEP = 1.6
#: Seconds between blinks, and how long the eyes stay shut.
BLINK_PERIOD = 4.3
BLINK_LENGTH = 0.18
#: Width (columns) of the shine's soft highlight band.
_SHINE_RADIUS = 5.0


def wordmark_rows(text: str = "phoson-cli") -> list[str]:
    """``text`` in the pixel font: five rows of full blocks."""
    rows = ["", "", "", "", ""]
    for i, char in enumerate(text):
        for row, pixels in enumerate(_GLYPHS[char]):
            rows[row] += (" " if i else "") + pixels.replace("#", "█").replace(".", " ")
    return rows


_WORDMARK = wordmark_rows()
WORDMARK_WIDTH = len(_WORDMARK[0])


def animations_enabled(theme_name: str) -> bool:
    """False for the no-color tier and when the user opted out of motion."""
    if theme_name == "no-color":
        return False
    return not (os.environ.get("NO_COLOR") or os.environ.get("PHOSON_NO_ANIMATION"))


def _shine_center(t: float) -> float | None:
    """Wordmark column the shine is centered on at time *t* (None = paused)."""
    phase = t % SHINE_PERIOD
    if phase >= SHINE_SWEEP:
        return None
    span = WORDMARK_WIDTH + 2 * _SHINE_RADIUS
    return phase / SHINE_SWEEP * span - _SHINE_RADIUS


def _blinking(t: float) -> bool:
    return (t % BLINK_PERIOD) < BLINK_LENGTH


def banner_frame_key(t: float | None) -> tuple[object, ...]:
    """Changes exactly when the banner's picture does (repaint dedupe)."""
    if t is None:
        return ("static",)
    center = _shine_center(t)
    return (None if center is None else round(center), _blinking(t))


def _rgb(color: str) -> tuple[int, int, int] | None:
    try:
        triplet = Color.parse(color).get_truecolor()
    except (ColorParseError, ValueError):
        return None
    return triplet.red, triplet.green, triplet.blue


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], f: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(round(x + (y - x) * f) for x, y in zip(a, b)))


class PhosonBanner:
    """Logo + ``phoson-cli`` wordmark; animated when *t* is not ``None``.

    *t* is the time in seconds since the banner first appeared. Below
    :data:`LOGO_WIDTH` + :data:`WORDMARK_WIDTH` + padding columns the pixel
    wordmark is dropped and the plain-text line carries the name alone.
    """

    def __init__(self, theme, version: str, t: float | None = None) -> None:
        self.theme = theme
        self.version = version
        self.t = t

    def _wordmark_line(self, row: str, base: str) -> Text:
        line = Text()
        accent = _rgb(base) if base else None
        center = _shine_center(self.t) if self.t is not None else None
        if accent is None or center is None:
            line.append(row, style=f"bold {base}".strip())
            return line
        white = (255, 255, 255)
        for x, char in enumerate(row):
            glow = math.exp(-(((x - center) / (_SHINE_RADIUS / 2)) ** 2))
            line.append(char, style=f"bold {_mix(accent, white, 0.75 * glow)}")
        return line

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        theme = self.theme
        eyes = _EYES_SHUT if self.t is not None and _blinking(self.t) else _EYES_OPEN
        logo = [row.replace("{eyes}", eyes) for row in _LOGO]
        tagline = Text()
        tagline.append("phoson-cli", style=f"bold {theme.accent}".strip())
        tagline.append(f"  v{self.version}  ·  terminal agent", style=theme.muted)

        wide = options.max_width >= 2 + LOGO_WIDTH + 4 + WORDMARK_WIDTH
        # Right-hand column, aligned to the logo's rows: the wordmark on rows
        # 0-4, the tagline under it (row 6).
        right: dict[int, Text] = {}
        if wide:
            for i, row in enumerate(_WORDMARK):
                right[i] = self._wordmark_line(row, theme.accent)
            right[6] = tagline
        for i, row in enumerate(logo):
            line = Text("  ")
            line.append(row, style=theme.art)
            if i in right:
                line.append("    ")
                line.append_text(right[i])
            yield line
        if not wide:
            # Narrow terminal: no pixel wordmark, the name goes under the face.
            yield Text("  ").append_text(tagline)


__all__ = [
    "LOGO_WIDTH",
    "WORDMARK_WIDTH",
    "PhosonBanner",
    "animations_enabled",
    "banner_frame_key",
    "wordmark_rows",
]
