"""macOS backend: Quartz ``CGEvent`` input + ``screencapture`` capture.

Requires the process to hold **Screen Recording** (capture) and **Accessibility**
(input) grants in System Settings → Privacy & Security. TCC grants are tied to
the app/executable identity, so a different terminal or interpreter may prompt
again.

This backend is **not exercised in CI** (no macOS runner); treat it as beta and
validate on real hardware before relying on it.
"""

import time
import tempfile
import subprocess
from pathlib import Path

from .base import RasterImage, ComputerBackend, ComputerUseError
from ..geometry import Region

QUARTZ_AVAILABLE = True
_QUARTZ_IMPORT_ERROR: Exception | None = None

try:  # optional dependency (macOS only)
    import Quartz  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - macOS only
    QUARTZ_AVAILABLE = False
    _QUARTZ_IMPORT_ERROR = exc


#: US-layout virtual keycodes for ``computer_key`` names.
_KEYCODES: dict[str, int] = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "backspace": 51,
    "delete": 51,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "ctrl": 59,
    "control": 59,
    "alt": 58,
    "option": 58,
    "shift": 56,
    "cmd": 55,
    "command": 55,
    "super": 55,
    "meta": 55,
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
}

_BUTTONS = (
    {
        "left": (
            Quartz.kCGMouseButtonLeft,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
            Quartz.kCGEventLeftMouseDragged,
        ),
        "right": (
            Quartz.kCGMouseButtonRight,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventRightMouseUp,
            Quartz.kCGEventRightMouseDragged,
        ),
        "middle": (
            Quartz.kCGMouseButtonCenter,
            Quartz.kCGEventOtherMouseDown,
            Quartz.kCGEventOtherMouseUp,
            Quartz.kCGEventOtherMouseDragged,
        ),
    }
    if QUARTZ_AVAILABLE
    else {}
)


class MacOSBackend(ComputerBackend):
    """Drive the macOS desktop through Quartz and ``screencapture``."""

    name = "macos"

    def __init__(self) -> None:
        if not QUARTZ_AVAILABLE:
            raise ComputerUseError(
                "The macOS backend needs 'pyobjc-framework-Quartz'. Install "
                "with: uv sync --extra computeruse-macos  (or: pip install "
                "'phoson-engine-minimal[computeruse-macos]'). "
                f"Original error: {_QUARTZ_IMPORT_ERROR}"
            )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ComputerUseError("macOS backend is closed")

    # ── capture ──────────────────────────────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        self._ensure_open()
        display = Quartz.CGMainDisplayID()
        return (
            int(Quartz.CGDisplayPixelsWide(display)),
            int(Quartz.CGDisplayPixelsHigh(display)),
        )

    def capture(self, region: Region | None = None) -> RasterImage:
        self._ensure_open()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            path = Path(handle.name)
        try:
            command = ["screencapture", "-x", "-t", "png"]
            if region is None:
                w, h = self.screen_size()
                command += ["-R", f"0,0,{w},{h}"]
            else:
                command += [
                    "-R",
                    f"{region.x},{region.y},{region.width},{region.height}",
                ]
            command.append(str(path))
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=15)
            except FileNotFoundError as exc:
                raise ComputerUseError("'screencapture' not found") from exc
            except subprocess.CalledProcessError as exc:
                raise ComputerUseError(
                    "screencapture failed (is Screen Recording granted?): "
                    f"{exc.stderr.decode('utf-8', 'replace').strip()}"
                ) from exc
            data = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        width, height = (
            self.screen_size() if region is None else (region.width, region.height)
        )
        return RasterImage(data=data, width=width, height=height)

    # ── input ────────────────────────────────────────────────────────────

    def _post_mouse(self, event_type: int, x: int, y: int, button: int) -> None:
        event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), button)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def move(self, x: int, y: int) -> None:
        self._ensure_open()
        self._post_mouse(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft)

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._ensure_open()
        spec = _BUTTONS.get(button)
        if spec is None:
            raise ComputerUseError(f"Unknown mouse button {button!r}")
        number, down, up, _ = spec
        if clicks < 1:
            raise ComputerUseError("clicks must be >= 1")
        self.move(x, y)
        for _ in range(clicks):
            self._post_mouse(down, x, y, number)
            self._post_mouse(up, x, y, number)
            if clicks > 1:
                time.sleep(0.05)

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3
    ) -> None:
        self._ensure_open()
        number, down, up, dragged = _BUTTONS["left"]
        steps = max(1, int(duration / 0.02))
        self.move(x1, y1)
        self._post_mouse(down, x1, y1, number)
        for step in range(1, steps + 1):
            ix = x1 + (x2 - x1) * step // steps
            iy = y1 + (y2 - y1) * step // steps
            self._post_mouse(dragged, ix, iy, number)
            time.sleep(duration / steps)
        self._post_mouse(up, x2, y2, number)

    def scroll(self, x: int, y: int, *, dx: int, dy: int) -> None:
        self._ensure_open()
        self.move(x, y)
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx)
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def type_text(self, text: str) -> None:
        self._ensure_open()
        for char in text:
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(char), char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def press(self, keys: list[str]) -> None:
        self._ensure_open()
        if not keys:
            raise ComputerUseError("keys must not be empty")
        codes: list[int] = []
        for name in keys:
            code = _KEYCODES.get(name.lower())
            if code is None:
                raise ComputerUseError(f"Unsupported key name: {name!r}")
            codes.append(code)
        for code in codes:
            Quartz.CGEventPost(
                Quartz.kCGHIDEventTap,
                Quartz.CGEventCreateKeyboardEvent(None, code, True),
            )
        for code in reversed(codes):
            Quartz.CGEventPost(
                Quartz.kCGHIDEventTap,
                Quartz.CGEventCreateKeyboardEvent(None, code, False),
            )

    def close(self) -> None:
        self._closed = True
