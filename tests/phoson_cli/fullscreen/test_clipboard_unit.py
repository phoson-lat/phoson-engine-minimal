"""Unit tests for clipboard image/text retrieval (Ctrl+V in the full-screen app).

Covers the three backends (Wayland, X11, macOS) and the text fallback
added in IMPROVEMENTS.md D3.
"""

import os
import sys
import signal
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.fullscreen.clipboard import (
    _run_command,
    _text_command,
    _image_command,
    read_clipboard_text,
    read_clipboard_image,
    macos_image_tool_hint,
    _await_process_cleanup,
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


async def test_clipboard_cancellation_kills_and_reaps_child(tmp_path) -> None:
    pid_file = tmp_path / "clipboard-child.pid"
    code = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, lambda *_: None); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    task = asyncio.create_task(_run_command([sys.executable, "-c", code]))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_repeated_cancellation_waits_through_term_kill_and_reap(
    monkeypatch,
) -> None:
    communicate_started = asyncio.Event()
    term_sent = asyncio.Event()

    exited = asyncio.Event()

    class Process:
        returncode = None
        stdout = None
        killed = False
        wait_calls = 0

        async def communicate(self):
            communicate_started.set()
            await asyncio.Event().wait()

        def terminate(self) -> None:
            term_sent.set()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            exited.set()

        async def wait(self) -> int:
            self.wait_calls += 1
            await exited.wait()
            return -9

    proc = Process()
    monkeypatch.setattr("phoson_cli.fullscreen.clipboard._PROCESS_EXIT_TIMEOUT", 0.01)
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        task = asyncio.create_task(_run_command(["clipboard-reader"]))
        await communicate_started.wait()
        task.cancel()
        await term_sent.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task

    assert proc.killed is True
    assert proc.wait_calls == 1


async def test_cleanup_defers_but_preserves_new_cancellation(monkeypatch) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def stop(proc):
        started.set()
        await finish.wait()

    monkeypatch.setattr("phoson_cli.fullscreen.clipboard._stop_process", stop)
    task = asyncio.create_task(_await_process_cleanup(MagicMock()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("exit_mode", ["term", "kill", "already_exited"])
async def test_cancelled_communicate_drains_full_pipe(
    tmp_path, monkeypatch, exit_mode
) -> None:
    """A real cancelled reader leaves a paused pipe, even after child exit.

    The file handshake delays a bounded burst until communicate's real reader
    has been cancelled. No clipboard tool, network or timing-based flood.
    """
    trigger = tmp_path / "write"
    code = f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.{"SIG_DFL" if exit_mode == "term" else "SIG_IGN"})
while not os.path.exists({str(trigger)!r}):
    time.sleep(0.001)
data = b'x' * (1024 * 1024)
while data:
    data = data[os.write(1, data):]
time.sleep(30)
"""
    before = asyncio.all_tasks()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=1024,
    )
    communicate = proc.communicate
    started = asyncio.Event()
    full = asyncio.Event()
    term = asyncio.Event()

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.001)

    async def controlled_communicate():
        started.set()
        try:
            return await communicate()
        except asyncio.CancelledError:
            trigger.touch()
            # White-box observation only: do not fake StreamReader backpressure.
            await wait_until(lambda: proc.stdout._paused)
            full.set()
            if exit_mode == "already_exited":
                proc.kill()
                await wait_until(lambda: proc.returncode is not None)
            raise

    terminate = proc.terminate

    def controlled_terminate():
        term.set()
        terminate()

    monkeypatch.setattr(proc, "communicate", controlled_communicate)
    monkeypatch.setattr(proc, "terminate", controlled_terminate)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    task = asyncio.create_task(_run_command(["controlled-local-child"]))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(full.wait(), 5)
        if exit_mode == "kill":
            await asyncio.wait_for(term.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
        # wait(), unlike wait_for(task), cannot hang cancelling broken cleanup.
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done, f"cleanup stuck with returncode={proc.returncode}"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None
        if exit_mode != "term":
            assert proc.returncode == -signal.SIGKILL
        assert proc.stdout.at_eof()
        assert proc._transport.is_closing()
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert not (asyncio.all_tasks() - before)
    finally:
        # Rescue even the unfixed implementation without leaving a child,
        # reader or cleanup task behind when the bounded assertion fails.
        if proc.returncode is None:
            proc.kill()
        proc._transport.get_pipe_transport(1).close()
        await asyncio.wait_for(proc.wait(), 5)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


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
