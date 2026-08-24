"""Clipboard image retrieval for the full-screen app's Ctrl+V paste.

Terminals only ever deliver *text* through their own paste mechanism —
getting an actual image out of the system clipboard requires shelling
out to the platform's clipboard tool directly, bypassing the terminal
entirely.
"""

import os
import shutil
import asyncio

_IMAGE_MIME_CANDIDATES = ("image/png", "image/jpeg")


def _clipboard_command(mime: str) -> list[str] | None:
    """Pick the clipboard-read command for the current session, if any."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return ["wl-paste", "--type", mime]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
    return None


async def read_clipboard_image() -> tuple[bytes, str] | None:
    """Return ``(bytes, mime)`` for an image on the system clipboard, or ``None``.

    Tries PNG then JPEG — what screenshot tools and browsers put on the
    clipboard — and returns ``None`` if no clipboard tool is available,
    the clipboard holds no image, or the read fails.
    """
    for mime in _IMAGE_MIME_CANDIDATES:
        command = _clipboard_command(mime)
        if command is None:
            return None
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
            return stdout, mime
    return None


__all__ = ["read_clipboard_image"]
