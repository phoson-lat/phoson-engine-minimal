"""Clipboard access (image + text) for the full-screen app's Ctrl+V.

Terminals only ever deliver *text* through their own paste mechanism —
getting an actual image out of the system clipboard requires shelling
out to the platform's clipboard tool directly, bypassing the terminal
entirely. Three backends are supported:

- **Wayland** — ``wl-paste`` (from ``wl-clipboard``).
- **X11** — ``xclip``.
- **macOS** — ``pngpaste`` for images (``brew install pngpaste``; no
  built-in macOS tool reads clipboard images directly) and the
  always-available ``pbpaste`` for text.

Text is read as a fallback (IMPROVEMENTS.md D3): Ctrl+V is bound to
image-paste, but most clipboard contents are text, not images — a
clipboard with text and no image must still paste that text rather
than silently doing nothing (``PhosonApp.paste_image`` tries
:func:`read_clipboard_image` first, then :func:`read_clipboard_text`).

Copy mode (IMPROVEMENTS.md G3) writes the other way:
:func:`write_clipboard_text` yanks a selected chat range to the system
clipboard using the same platform tools (``wl-copy`` / ``xclip`` /
``pbcopy``). Those tools need a local display, so a bare SSH/remote
session has none — for that case :func:`write_clipboard_osc52` writes the
same text via the OSC 52 escape sequence, letting the *local* terminal
(which the user's eyes see) perform the copy; it is used as a fallback
when no platform tool is available (see :func:`osc52_enabled`).
"""

import os
import sys
import base64
import shutil
import asyncio

_IMAGE_MIME_CANDIDATES = ("image/png", "image/jpeg")

# Terminals known to honor an OSC 52 *write* (copy to system clipboard).
# Detection is best-effort from environment markers; it is deliberately
# conservative so we don't claim success on a terminal that will silently
# drop the sequence (e.g. VTE-based GNOME/Xfce terminals, rxvt-unicode,
# macOS Terminal.app, PuTTY). Users can force it with
# ``clipboard_osc52 = "on"`` (the common case it unblocks is SSH, where the
# local terminal's env markers aren't forwarded to the remote process).
#
#   TERM_PROGRAM values (lower-cased, also accepts the space-free form):
#   WezTerm, kitty, Ghostty, iTerm.app, WindowsTerminal
#   Distinctive $TERM values: kitty, alacritty, foot, ghostty, st, contour
#   Per-terminal env vars: KITTY_WINDOW_ID, ALACRITTY_INSTANCE_ID
_OSC52_TERM_PROGRAMS = {
    "wezterm",
    "kitty",
    "ghostty",
    "iterm.app",
    "iterm2",
    "windows terminal",
    "windowsterminal",
}
_OSC52_TERMS = {"kitty", "alacritty", "foot", "ghostty", "st", "contour"}


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _wayland_available() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"))


def _x11_available() -> bool:
    return bool(os.environ.get("DISPLAY") and shutil.which("xclip"))


def _image_command(mime: str) -> list[str] | None:
    """Pick the clipboard-read command for an image of ``mime``, if any."""
    if _wayland_available():
        return ["wl-paste", "--type", mime]
    if _x11_available():
        return ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
    if _is_macos() and mime == "image/png" and shutil.which("pngpaste"):
        # pngpaste always converts to PNG regardless of the clipboard's
        # original format, so it is only tried once (on the "image/png"
        # pass of the mime loop below).
        return ["pngpaste", "-"]
    return None


def _text_command() -> list[str] | None:
    """Pick the clipboard-read command for plain text, if any."""
    if _wayland_available():
        return ["wl-paste", "--no-newline"]
    if _x11_available():
        return ["xclip", "-selection", "clipboard", "-o"]
    if _is_macos() and shutil.which("pbpaste"):
        return ["pbpaste"]
    return None


def _write_command() -> list[str] | None:
    """Pick the clipboard-write command for plain text, if any.

    The write side checks its *own* tools (``wl-copy`` / ``xclip`` /
    ``pbcopy``) rather than reusing the read-side availability helpers,
    because the read and write tools differ (e.g. Wayland reads with
    ``wl-paste`` but writes with ``wl-copy`` — same package, but the gate
    must name the tool it actually shells out to).
    """
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if _is_macos() and shutil.which("pbcopy"):
        return ["pbcopy"]
    return None


async def write_clipboard_text(text: str) -> bool:
    """Write *text* to the system clipboard; ``True`` on success.

    The inverse of :func:`read_clipboard_text` — used by the full-screen
    copy mode (IMPROVEMENTS.md G3) to yank a selected chat range. ``False``
    when no clipboard tool is available (e.g. a bare SSH session with no
    ``xclip``/``wl-copy``/``pbcopy``) or the write fails; callers surface a
    notice so the user knows the selection was *not* copied.
    """
    command = _write_command()
    if command is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(text.encode("utf-8"))
    except OSError:
        return False
    return proc.returncode == 0


def clipboard_write_available() -> bool:
    """Whether a clipboard-write tool is present for this session."""
    return _write_command() is not None


def clipboard_write_hint() -> str | None:
    """A short install hint when no clipboard-write tool is available.

    ``None`` when a writer *is* available (nothing to hint about).
    """
    if _write_command() is not None:
        return None
    if _is_macos():
        return "macOS clipboard writes use the built-in pbcopy"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "install wl-clipboard for Wayland clipboard writes (wl-copy)"
    if os.environ.get("DISPLAY"):
        return "install xclip for X11 clipboard writes"
    return "no clipboard tool found (install xclip or wl-clipboard)"


def macos_image_tool_hint() -> str | None:
    """Install hint for image paste on macOS without ``pngpaste``.

    ``None`` when not on macOS, or when ``pngpaste`` is already
    installed — callers only show this alongside the "no image on the
    clipboard" notice, and only when it explains *why*.
    """
    if _is_macos() and not shutil.which("pngpaste"):
        return "install pngpaste for image paste on macOS: brew install pngpaste"
    return None


# ── OSC 52 (terminal-mediated clipboard, IMPROVEMENTS.md G3 follow-up) ────────
#
# OSC 52 is the de-facto standard (Neovim, Emacs, tmux, nano, kitty, …) for a
# terminal *application* to write to the system clipboard *through* the
# terminal — no ``xclip``/``wl-copy``/`pbcopy` required. That is exactly the
# gap the platform tools leave: a bare SSH/remote session has no local
# display, so no clipboard tool, and only the local terminal can actually put
# bytes on the clipboard. The application emits the sequence; the *local*
# terminal (which the user's eyes see) performs the copy.
#
# Write form:  ESC ] 52 ; c ; <base64(text)> ST
#   ST (string terminator) is ESC \ — the modern, unambiguous terminator
#   (a literal BEL 0x07 also works but is easier to confuse with a real bell).
# ``c`` = system clipboard (the target that Ctrl+V pastes from everywhere).


def osc52_sequence(text: str) -> str:
    """Build the OSC 52 escape sequence that copies *text* to the clipboard.

    Pure function (no I/O) so the exact byte sequence is unit-testable and
    can be reused by anything that has a terminal output to write to.
    """
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{encoded}\x1b\\"


def osc52_supported() -> bool:
    """Best-effort detection of OSC 52 *write* support from the environment.

    Only the terminals whose env markers we positively recognize are
    accepted, so a terminal that silently drops the sequence (VTE-based
    GNOME/Xfce terminals, macOS Terminal.app, rxvt-unicode, PuTTY) is not
    falsely reported as supported. The per-terminal variables
    (``KITTY_WINDOW_ID``, ``ALACRITTY_INSTANCE_ID``) are the strongest
    signal; ``TERM_PROGRAM`` and a distinctive ``$TERM`` follow.
    """
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("ALACRITTY_INSTANCE_ID"):
        return True
    program = (os.environ.get("TERM_PROGRAM") or "").lower()
    if program:
        program_nospace = program.replace(" ", "")
        if program in _OSC52_TERM_PROGRAMS or any(
            program == name or program_nospace == name.replace(" ", "")
            for name in _OSC52_TERM_PROGRAMS
        ):
            return True
    term = (os.environ.get("TERM") or "").lower()
    if term:
        # Terminals use different $TERM conventions for the same name —
        # kitty/ghostty are "xterm-<name>", st is "<name>-256color",
        # alacritty/foot/contour are the bare name. Match any "-" component
        # so all three forms are recognized without false positives on
        # unrelated terms like "xterm-256color".
        components = term.split("-")
        if any(comp in _OSC52_TERMS for comp in components):
            return True
    return False


def osc52_enabled(config_value: str | None = None) -> bool:
    """Resolve the ``clipboard_osc52`` setting into a boolean.

    ``"on"`` forces the OSC 52 fallback on regardless of detection;
    ``"off"`` forces it off; ``"auto"``/``None`` falls back to
    :func:`osc52_supported`. Unknown values are treated as ``"auto"``.
    """
    value = (config_value or "auto").strip().lower()
    if value == "on":
        return True
    if value == "off":
        return False
    return osc52_supported()


def write_clipboard_osc52(text: str) -> bool:
    """Write *text* to the clipboard via an OSC 52 sequence on ``/dev/tty``.

    Sends the sequence to the controlling terminal, which — if it supports
    OSC 52 — places the decoded text on the system clipboard. Returns
    ``True`` when the bytes were handed to the tty (the terminal then
    decides whether it honors them), ``False`` when there is no controlling
    tty (e.g. the app was started piped) so the caller can fall through to
    the platform tools. The write is a single small ``os.write`` — the same
    mechanism tmux/Emacs use — so it is safe to call while the TUI owns the
    terminal.
    """
    if not text:
        return False
    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
    except OSError:
        return False
    try:
        os.write(fd, osc52_sequence(text).encode("utf-8"))
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


async def _run_command(command: list[str]) -> bytes | None:
    """Run *command*, returning its stdout bytes, or ``None`` on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode == 0 and stdout:
        return stdout
    return None


async def read_clipboard_image() -> tuple[bytes, str] | None:
    """Return ``(bytes, mime)`` for an image on the system clipboard, or ``None``.

    Tries PNG then JPEG — what screenshot tools and browsers put on the
    clipboard — and returns ``None`` if no clipboard tool is available,
    the clipboard holds no image, or the read fails.
    """
    for mime in _IMAGE_MIME_CANDIDATES:
        command = _image_command(mime)
        if command is None:
            return None
        data = await _run_command(command)
        if data:
            return data, mime
    return None


async def read_clipboard_text() -> str | None:
    """Return the system clipboard's plain-text contents, or ``None``.

    ``None`` when no clipboard tool is available, the clipboard is
    empty, or its contents are not valid UTF-8 text (e.g. it holds an
    image the caller already tried via :func:`read_clipboard_image`).
    """
    command = _text_command()
    if command is None:
        return None
    data = await _run_command(command)
    if not data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


__all__ = [
    "read_clipboard_image",
    "read_clipboard_text",
    "write_clipboard_text",
    "clipboard_write_available",
    "clipboard_write_hint",
    "macos_image_tool_hint",
    "osc52_sequence",
    "osc52_supported",
    "osc52_enabled",
    "write_clipboard_osc52",
]
