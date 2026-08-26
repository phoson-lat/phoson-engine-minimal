"""Unit tests for clipboard image/text retrieval (Ctrl+V in the full-screen app).

Covers the three backends (Wayland, X11, macOS) and the text fallback
added in IMPROVEMENTS.md D3.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from phoson_cli.fullscreen.clipboard import (
    _text_command,
    _image_command,
    read_clipboard_text,
    read_clipboard_image,
    macos_image_tool_hint,
)

# ── Image command selection ──────────────────────────────────────────────────


def test_clipboard_command_prefers_wl_paste_on_wayland() -> None:
    with (
        patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/wl-paste"),
    ):
        command = _image_command("image/png")
    assert command == ["wl-paste", "--type", "image/png"]


def test_clipboard_command_falls_back_to_xclip_on_x11() -> None:
    with (
        patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/xclip"),
    ):
        command = _image_command("image/png")
    assert command == ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]


def test_clipboard_command_falls_back_to_xclip_when_wl_paste_missing() -> None:
    """Wayland session (WAYLAND_DISPLAY set) but wl-paste isn't installed —

    common when only XWayland compatibility is available — must still
    resolve to xclip if DISPLAY is also set and xclip exists.
    """

    def fake_which(name: str) -> str | None:
        return "/usr/bin/xclip" if name == "xclip" else None

    with (
        patch.dict(
            "os.environ", {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, clear=True
        ),
        patch("shutil.which", side_effect=fake_which),
    ):
        command = _image_command("image/png")
    assert command == ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]


def test_clipboard_command_none_when_no_tool_available() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value=None),
        patch("sys.platform", "linux"),
    ):
        assert _image_command("image/png") is None


# ── macOS backend (D3) ────────────────────────────────────────────────────────


def test_clipboard_command_uses_pngpaste_on_macos() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value="/usr/local/bin/pngpaste"),
        patch("sys.platform", "darwin"),
    ):
        command = _image_command("image/png")
    assert command == ["pngpaste", "-"]


def test_clipboard_command_none_on_macos_for_jpeg() -> None:
    """pngpaste only ever emits PNG — the JPEG pass of the mime loop must

    not resolve to it (avoids trying the same tool twice for two mimes)."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value="/usr/local/bin/pngpaste"),
        patch("sys.platform", "darwin"),
    ):
        assert _image_command("image/jpeg") is None


def test_clipboard_command_none_on_macos_without_pngpaste() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value=None),
        patch("sys.platform", "darwin"),
    ):
        assert _image_command("image/png") is None


def test_text_command_uses_pbpaste_on_macos() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value="/usr/bin/pbpaste"),
        patch("sys.platform", "darwin"),
    ):
        assert _text_command() == ["pbpaste"]


def test_macos_image_tool_hint_when_pngpaste_missing() -> None:
    with (
        patch("shutil.which", return_value=None),
        patch("sys.platform", "darwin"),
    ):
        hint = macos_image_tool_hint()
    assert hint is not None
    assert "pngpaste" in hint
    assert "brew install" in hint


def test_macos_image_tool_hint_none_when_pngpaste_present() -> None:
    with (
        patch("shutil.which", return_value="/usr/local/bin/pngpaste"),
        patch("sys.platform", "darwin"),
    ):
        assert macos_image_tool_hint() is None


def test_macos_image_tool_hint_none_on_linux() -> None:
    with patch("sys.platform", "linux"):
        assert macos_image_tool_hint() is None


# ── Text command selection ───────────────────────────────────────────────────


def test_text_command_prefers_wl_paste_on_wayland() -> None:
    with (
        patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/wl-paste"),
    ):
        assert _text_command() == ["wl-paste", "--no-newline"]


def test_text_command_falls_back_to_xclip_on_x11() -> None:
    with (
        patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/xclip"),
    ):
        assert _text_command() == ["xclip", "-selection", "clipboard", "-o"]


def test_text_command_none_when_no_tool_available() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value=None),
        patch("sys.platform", "linux"),
    ):
        assert _text_command() is None


# ── read_clipboard_image ──────────────────────────────────────────────────────


async def test_read_clipboard_image_returns_bytes_on_success() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"pngbytes", b""))
    fake_proc.returncode = 0

    with (
        patch(
            "phoson_cli.fullscreen.clipboard._image_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
    ):
        result = await read_clipboard_image()

    assert result == (b"pngbytes", "image/png")


async def test_read_clipboard_image_tries_jpeg_when_png_empty() -> None:
    png_proc = MagicMock()
    png_proc.communicate = AsyncMock(return_value=(b"", b""))
    png_proc.returncode = 1
    jpeg_proc = MagicMock()
    jpeg_proc.communicate = AsyncMock(return_value=(b"jpegbytes", b""))
    jpeg_proc.returncode = 0

    with (
        patch(
            "phoson_cli.fullscreen.clipboard._image_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[png_proc, jpeg_proc]),
        ),
    ):
        result = await read_clipboard_image()

    assert result == (b"jpegbytes", "image/jpeg")


async def test_read_clipboard_image_none_when_no_tool_available() -> None:
    with patch("phoson_cli.fullscreen.clipboard._image_command", return_value=None):
        assert await read_clipboard_image() is None


async def test_read_clipboard_image_none_on_subprocess_error() -> None:
    with (
        patch(
            "phoson_cli.fullscreen.clipboard._image_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("no such tool")),
        ),
    ):
        assert await read_clipboard_image() is None


# ── read_clipboard_text (D3) ─────────────────────────────────────────────────


async def test_read_clipboard_text_returns_decoded_string() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hello clipboard", b""))
    fake_proc.returncode = 0

    with (
        patch(
            "phoson_cli.fullscreen.clipboard._text_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
    ):
        assert await read_clipboard_text() == "hello clipboard"


async def test_read_clipboard_text_none_when_no_tool_available() -> None:
    with patch("phoson_cli.fullscreen.clipboard._text_command", return_value=None):
        assert await read_clipboard_text() is None


async def test_read_clipboard_text_none_when_empty() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0

    with (
        patch(
            "phoson_cli.fullscreen.clipboard._text_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
    ):
        assert await read_clipboard_text() is None


async def test_read_clipboard_text_none_on_invalid_utf8() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"\xff\xfe\x00\x01", b""))
    fake_proc.returncode = 0

    with (
        patch(
            "phoson_cli.fullscreen.clipboard._text_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
    ):
        assert await read_clipboard_text() is None
