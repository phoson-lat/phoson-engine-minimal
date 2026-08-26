"""Terminal light/dark detection for first-run theme suggestions (E4).

Not every terminal tells us its color scheme, so detection is layered and
each layer fails soft to ``None`` ("undetermined"):

1. ``COLORFGBG`` — exported by some shells/terminal multiplexers
   (``light;dark`` on tmux, ``0;15`` / ``15;0`` / ``15;15`` … as 16-color
   indexes on xterm-derived terminals).
2. OSC 11 query (``\\x1b]11;?\\x07``) — the terminal reports its default
   *background* color; many modern terminals (iTerm2, kitty, WezTerm,
   Alacritty, ghostty, VS Code, …) answer with an sRGB color.

Nothing here ever raises: detection is a startup nicety, and an odd
terminal (no response, malformed response, no TTY) must degrade to
"ask the user" rather than a traceback. The only synchronous syscall is
the OSC 11 read, which is bounded by a ~150 ms timeout.
"""

import os
import re
import select
import warnings
from collections.abc import Callable

try:
    import tty as _tty
except ImportError:  # pragma: no cover - non-POSIX platforms (Windows)
    _tty = None  # type: ignore[assignment]

try:
    import termios
except ImportError:  # pragma: no cover - non-POSIX platforms (Windows)
    termios = None  # type: ignore[assignment]

# Relative luminance (WCAG) — the same perceptual scale color-contrast
# tools use. 0.5 is a pragmatic light/dark boundary: nearly every real
# light theme sits above ~0.7 and every dark theme below ~0.35.
_LUMA_THRESHOLD = 0.5

# OSC 11 response: ESC ] 11 ; <color> (ESC \ or BEL). Group 1 is the color.
_OSC_11_RE = re.compile(r"\x1b\]11;([^\x07\x1b]*?)(\x1b\\|\x07)")

#: 8 base ANSI background colors (SGR 40–47) → (r, g, b). Values from the
#: classic xterm/PC palette; good enough for a light/dark *guess*.
_ANSI_BG_RGB: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 0),
    1: (205, 0, 0),
    2: (0, 205, 0),
    3: (205, 205, 0),
    4: (0, 0, 238),
    5: (205, 0, 205),
    6: (0, 205, 238),
    7: (229, 229, 229),
}


def _rgb_to_luma(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _is_light_rgb(rgb: tuple[int, int, int] | None) -> bool | None:
    """Classify an sRGB triple as light / dark / unknown (None)."""
    if rgb is None:
        return None
    return _rgb_to_luma(rgb) >= _LUMA_THRESHOLD


def _parse_color_token(token: str) -> tuple[int, int, int] | None:
    """Parse one sRGB color token the way terminals serialize it.

    Accepted shapes: ``#rrggbb``, ``rgb:<r>,<g>,<b>`` (255-based), and the
    bare decimal ``<r>;<g>;<b>`` form some terminals emit. Anything else
    (8-bit pairs, ``#rgb`` shorthand, …) returns ``None`` — a wrong guess
    is worse than no guess.
    """
    token = token.strip()
    if token.startswith("#") and len(token) == 7:
        try:
            return (
                int(token[1:3], 16),
                int(token[3:5], 16),
                int(token[5:7], 16),
            )
        except ValueError:
            return None
    body = token[4:] if token.startswith("rgb:") else token
    parts = [p for p in body.split(",") if p]
    if len(parts) != 3:
        parts = [p for p in body.split(";") if p]
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def parse_colorfgbg(value: str | None) -> bool | None:
    """Classify a ``COLORFGBG`` value's *background* half as light/dark.

    Returns ``None`` (undetermined) for anything unrecognized.
    """
    if not value:
        return None
    value = value.strip()
    # tmux-style literal "light;dark" / "dark;light".
    halves = [h.strip().lower() for h in value.split(";")]
    if any(h in {"light", "dark"} for h in halves):
        if len(halves) >= 2:
            return halves[1] == "light"
        return halves[0] == "light"
    # 16-color index form: "fg;bg" — only the background half matters.
    parts = value.split(";")
    bg_raw = parts[1] if len(parts) >= 2 else parts[0]
    try:
        idx = int(bg_raw.strip())
    except ValueError:
        return None
    if 0 <= idx <= 7:
        return _is_light_rgb(_ANSI_BG_RGB[idx])
    if 8 <= idx <= 15:
        # Bright variants are all "light" (bright_black ≈ grey, the rest
        # are saturated pastels).
        return True
    return None


def parse_osc11_response(raw: bytes) -> bool | None:
    """Extract the light/dark classification from raw OSC 11 reply bytes.

    ``raw`` is whatever ``os.read`` returned after the OSC 11 query —
    usually the terminal's single response, but it can be prefixed or
    suffixed with other escape sequences (including the query itself,
    echoed back), so every response block is scanned and the first one
    carrying a parseable color wins.
    """
    text = raw.decode("utf-8", errors="replace")
    for m in _OSC_11_RE.finditer(text):
        light = _is_light_rgb(_parse_color_token(m.group(1)))
        if light is not None:
            return light
    return None


def query_terminal_bg_light(
    timeout: float = 0.15,
    tty_fd: int | None = None,
    write: Callable[[bytes], None] | None = None,
    read: Callable[[], bytes] | None = None,
) -> bool | None:
    """Ask the terminal for its default background color (OSC 11).

    Sends ``\\x1b]11;?\\x07`` and waits up to *timeout* seconds for the
    response. Returns ``True`` (light) / ``False`` (dark) / ``None``
    (no usable response — non-TTY, terminal without support, timeout).

    The IO is injectable (``tty_fd``/``write``/``read``) so tests can
    drive it with in-memory bytes; by default it writes to fd 1 and
    reads fd 0. On the default path the tty is switched to raw mode for
    the duration of the probe (canonical mode would line-buffer the
    reply away) and restored afterwards.
    """
    injected_write = write
    injected_read = read
    if tty_fd is None:
        if not os.isatty(1) or not os.isatty(0):
            return None
        tty_fd = 0

    # A valid tty_fd that is not actually a TTY (closed, -1, …) just
    # yields no response — the probe is best-effort.
    try:
        if not os.isatty(tty_fd):
            return None
    except OSError:
        return None

    if write is None:

        def _write_stdout(data: bytes) -> None:
            os.write(1, data)

        write = _write_stdout

    if read is None:

        def _read_fd() -> bytes:
            return os.read(tty_fd, 256)

        read = _read_fd

    # Default-IO path only: a tty in canonical (line-buffered) mode would
    # swallow the single-line OSC 11 reply — the response contains no
    # newline, so the read would block until something else arrives.
    # Raw mode for the duration of the probe; injected IO (tests) skips
    # this because it owns the fd's discipline itself.
    saved_attrs = None
    if (
        termios is not None
        and _tty is not None
        and injected_write is None
        and injected_read is None
    ):
        try:
            saved_attrs = termios.tcgetattr(tty_fd)
            _tty.setraw(tty_fd)
        except (termios.error, OSError):
            saved_attrs = None

    try:
        write(b"\x1b]11;?\x07")
        ready, _, _ = select.select([tty_fd], [], [], timeout)
        if not ready:
            return None
        return parse_osc11_response(read())
    except OSError:
        return None
    except Exception as exc:  # pragma: no cover - defensive: never crash startup
        warnings.warn(f"OSC 11 theme probe failed: {exc}", stacklevel=2)
        return None
    finally:
        if saved_attrs is not None and termios is not None:
            try:
                termios.tcsetattr(tty_fd, termios.TCSANOW, saved_attrs)
            except (termios.error, OSError):
                pass


def detect_terminal_theme() -> bool | None:
    """Best-effort guess that the terminal's background is light (E4).

    Order: ``COLORFGBG`` env → OSC 11 query. Returns ``None`` when the
    terminal cannot be classified and the caller should ask the user.
    """
    light = parse_colorfgbg(os.environ.get("COLORFGBG"))
    if light is not None:
        return light
    return query_terminal_bg_light()


__all__ = [
    "parse_colorfgbg",
    "parse_osc11_response",
    "query_terminal_bg_light",
    "detect_terminal_theme",
]
