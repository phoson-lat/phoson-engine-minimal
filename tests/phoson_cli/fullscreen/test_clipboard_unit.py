"""Unit tests for clipboard image retrieval (Ctrl+V paste in the full-screen app)."""

from unittest.mock import AsyncMock, MagicMock, patch

from phoson_cli.fullscreen.clipboard import _clipboard_command, read_clipboard_image


def test_clipboard_command_prefers_wl_paste_on_wayland() -> None:
    with (
        patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/wl-paste"),
    ):
        command = _clipboard_command("image/png")
    assert command == ["wl-paste", "--type", "image/png"]


def test_clipboard_command_falls_back_to_xclip_on_x11() -> None:
    with (
        patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/xclip"),
    ):
        command = _clipboard_command("image/png")
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
        command = _clipboard_command("image/png")
    assert command == ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]


def test_clipboard_command_none_when_no_tool_available() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value=None),
    ):
        assert _clipboard_command("image/png") is None


async def test_read_clipboard_image_returns_bytes_on_success() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"pngbytes", b""))
    fake_proc.returncode = 0

    with (
        patch(
            "phoson_cli.fullscreen.clipboard._clipboard_command",
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
            "phoson_cli.fullscreen.clipboard._clipboard_command",
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
    with patch(
        "phoson_cli.fullscreen.clipboard._clipboard_command", return_value=None
    ):
        assert await read_clipboard_image() is None


async def test_read_clipboard_image_none_on_subprocess_error() -> None:
    with (
        patch(
            "phoson_cli.fullscreen.clipboard._clipboard_command",
            return_value=["xclip", "-o"],
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("no such tool")),
        ),
    ):
        assert await read_clipboard_image() is None
