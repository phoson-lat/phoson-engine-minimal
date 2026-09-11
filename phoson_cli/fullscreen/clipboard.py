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
"""

import os
import sys
import uuid
import shutil
import asyncio
import tempfile
import mimetypes
from typing import Any
from pathlib import Path

from ..attachments import provider_compat_warning

_IMAGE_MIME_CANDIDATES = ("image/png", "image/jpeg")


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


def macos_image_tool_hint() -> str | None:
    """Install hint for image paste on macOS without ``pngpaste``.

    ``None`` when not on macOS, or when ``pngpaste`` is already
    installed — callers only show this alongside the "no image on the
    clipboard" notice, and only when it explains *why*.
    """
    if _is_macos() and not shutil.which("pngpaste"):
        return "install pngpaste for image paste on macOS: brew install pngpaste"
    return None


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


async def paste_image_from_clipboard(
    app: Any,
) -> None:
    """Read an image (or fallback text) from the clipboard and attach/insert it."""
    result = await read_clipboard_image()
    if result is None:
        await _paste_text_fallback(app)
        return

    data, mime = result
    suffix = mimetypes.guess_extension(mime) or ".png"
    target_dir = Path(tempfile.gettempdir()) / "phoson-clipboard"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"clipboard-{uuid.uuid4().hex[:8]}{suffix}"
    target.write_bytes(data)

    try:
        app.repl.attachments.attach(str(target))
    except (FileNotFoundError, ValueError) as exc:
        app.sink.notify("error", str(exc))
        return

    suffix = target.suffix.lower()
    warning = provider_compat_warning(
        suffix, getattr(app.repl.config, "provider", None)
    )
    if warning:
        app.sink.notify("warn", warning)

    placeholder = f"[image #{len(app.repl.attachments)}] "
    app._prompt_input.buffer.insert_text(placeholder)
    app.app.invalidate()


async def _paste_text_fallback(app: Any) -> None:
    """No image on the clipboard: paste its text instead (D3), if any."""
    text = await read_clipboard_text()
    if text:
        app._prompt_input.buffer.insert_text(text)
        app.app.invalidate()
        return

    message = "No image on the clipboard (or no clipboard tool available)."
    hint = macos_image_tool_hint()
    if hint:
        message = f"{message} {hint}."
    app.sink.notify("warn", message)


__all__ = [
    "read_clipboard_image",
    "read_clipboard_text",
    "macos_image_tool_hint",
    "paste_image_from_clipboard",
]
