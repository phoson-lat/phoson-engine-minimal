"""Windows backend: GDI ``BitBlt`` capture + ``SendInput`` HID input.

Runs as an ordinary userspace process — no admin rights, no extra service. Two
hard platform limits are worth knowing before relying on it:

- **UIPI**: a non-elevated process cannot inject input into an elevated window.
  ``SendInput`` then reports fewer events than requested and ``GetLastError``
  says why; this backend surfaces that as a :class:`ComputerUseError`.
- **Secure Desktop / lock screen**: UAC prompts, Ctrl+Alt+Del and a locked
  session are out of reach for any userspace process.

Coordinates follow the backend contract: absolute pixels in the **primary
monitor's** space, origin at its top-left (``(0, 0)``). Computer Use is scoped
to the primary monitor only — ``SendInput`` is asked to map onto it (no
``MOUSEEVENTF_VIRTUALDESK``) — so the model never has to reason about
multi-monitor offsets or a negative virtual-screen origin.

Pillow is the only dependency (PNG encoding), matching the ``computeruse-windows``
extra.
"""

import sys
import time
from io import BytesIO

from .base import RasterImage, ComputerBackend, ComputerUseError
from ..geometry import Region

_IS_WINDOWS = sys.platform == "win32"

# ── Win32 constants (winuser.h / wingdi.h) ───────────────────────────────

#: ``GetSystemMetrics`` indices for the primary monitor.
SM_CXSCREEN = 0
SM_CYSCREEN = 1

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

#: One wheel notch, per the Win32 docs (``WHEEL_DELTA``).
WHEEL_DELTA = 120

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

#: ``MOUSEEVENTF_*`` down/up flags per contract button name.
_BUTTONS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# ── Pure helpers (importable and unit-tested on any platform) ────────────

#: Named keys → virtual-key codes (layout-independent subset).
_VK: dict[str, int] = {
    "return": 0x0D,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "prior": 0x21,
    "pagedown": 0x22,
    "next": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "super": 0x5B,
    "cmd": 0x5B,
    "meta": 0x5B,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "pause": 0x13,
}
_VK.update({f"f{n}": 0x70 + (n - 1) for n in range(1, 25)})
_VK.update({str(d): 0x30 + d for d in range(10)})
_VK.update({chr(c): 0x41 + (c - ord("a")) for c in range(ord("a"), ord("z") + 1)})

#: Keys that the OS treats as extended (require ``KEYEVENTF_EXTENDEDKEY``).
_EXTENDED: frozenset[int] = frozenset(
    {0x26, 0x28, 0x25, 0x27, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22}
)

#: ``shift`` virtual-key, pressed when a character needs it.
_VK_SHIFT = 0x10


def normalize_absolute(value: int, origin: int, extent: int) -> int:
    """Map a display pixel to ``SendInput``'s 0..65535 space.

    ``MOUSEEVENTF_ABSOLUTE`` expects normalized coordinates over the target
    display surface — the primary monitor, here: ``(0, 0)`` is its top-left
    corner and ``(65535, 65535)`` its bottom-right. Using ``extent - 1`` makes
    the last pixel land exactly on 65535 instead of one short.
    """
    if extent <= 1:
        return 0
    scaled = round((int(value) - int(origin)) * 65535 / (int(extent) - 1))
    return max(0, min(65535, scaled))


def wheel_amount(notches: int, *, horizontal: bool = False) -> int:
    """Convert wheel notches to ``mouseData`` (multiples of ``WHEEL_DELTA``).

    The plugin contract is "positive ``dy`` scrolls down, positive ``dx``
    scrolls right". A positive vertical ``mouseData`` means the wheel rotated
    *away* from the user (scroll up), so the vertical sign is flipped;
    horizontal ``mouseData`` is already positive-to-the-right.
    """
    amount = int(notches) * WHEEL_DELTA
    return amount if horizontal else -amount


def virtual_key(name: str) -> int | None:
    """Return the virtual-key code for a named key, or ``None`` if unknown."""
    return _VK.get(name.lower())


def resolve_keys(name: str) -> list[tuple[int, bool]]:
    """Resolve a key name to the keys to hold, as ``(vk, extended)`` pairs.

    Named keys map straight through. A single printable character that is not
    in the table falls back to ``VkKeyScanW`` so punctuation (``/``, ``:``, …)
    works, expanding to a leading ``shift`` when the layout requires it.

    Windows-only (uses ``user32``); :func:`virtual_key` covers the portable
    subset for tests.
    """
    vk = virtual_key(name)
    if vk is not None:
        return [(vk, vk in _EXTENDED)]
    if len(name) == 1 and _IS_WINDOWS:
        scan = user32.VkKeyScanW(ord(name))
        if scan != -1:  # 0xFFFF as a signed SHORT means "no such key"
            code = scan & 0xFF
            needs_shift = bool(scan >> 8 & 1)
            result: list[tuple[int, bool]] = []
            if needs_shift:
                result.append((_VK_SHIFT, False))
            result.append((code, code in _EXTENDED))
            return result
    raise ComputerUseError(f"Unsupported key name: {name!r}")


# ── ctypes bindings (Windows only) ───────────────────────────────────────

if _IS_WINDOWS:  # pragma: no cover - exercised only on the Windows runner
    import ctypes
    from ctypes import wintypes

    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", _BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    # ``WinDLL`` and ``get_last_error`` are Windows-only members, absent from
    # the Linux stubs pyright uses in CI; alias them so the ignore stays local.
    _WinDLL = ctypes.WinDLL  # pyright: ignore[reportAttributeAccessIssue]
    _get_last_error = ctypes.get_last_error  # pyright: ignore[reportAttributeAccessIssue]

    user32 = _WinDLL("user32", use_last_error=True)
    gdi32 = _WinDLL("gdi32", use_last_error=True)

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    #: ``DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2`` (a negative "handle").
    _DPI_PER_MONITOR_V2 = ctypes.c_void_p(-4)


def _ensure_dpi_aware() -> None:
    """Best-effort per-monitor-v2 DPI awareness (process-global, once).

    The manifest is the documented way to set this, but a plugin cannot ship
    one for the host executable. Calling the API after an ``HWND`` exists is not
    supported and simply fails, which is fine: capture and ``SendInput`` still
    share the same ``SM_CXSCREEN``/``SM_CYSCREEN`` space, so coordinates stay
    consistent.
    """
    for attempt in (
        lambda: user32.SetProcessDpiAwarenessContext(_DPI_PER_MONITOR_V2),
        lambda: _WinDLL("shcore").SetProcessDpiAwareness(2),
        lambda: user32.SetProcessDPIAware(),
    ):
        try:
            attempt()
            return
        except (AttributeError, OSError):
            continue


class WindowsBackend(ComputerBackend):
    """Drive the Windows desktop through GDI capture and ``SendInput``."""

    name = "windows"

    def __init__(self) -> None:
        if not _IS_WINDOWS:
            raise ComputerUseError(
                "The Windows backend only runs on Windows (got "
                f"{sys.platform!r}); use backend='fake' for dry runs."
            )
        _ensure_dpi_aware()
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ComputerUseError("Windows backend is closed")

    # ── helpers ──────────────────────────────────────────────────────────

    def _primary_screen(self) -> tuple[int, int]:
        """Return ``(width, height)`` of the primary monitor in native pixels."""
        return (
            user32.GetSystemMetrics(SM_CXSCREEN),
            user32.GetSystemMetrics(SM_CYSCREEN),
        )

    def _send(self, *inputs: "_INPUT") -> None:
        """Dispatch ``SendInput`` events, raising if any were not injected."""
        count = len(inputs)
        array = (_INPUT * count)(*inputs)
        sent = user32.SendInput(count, ctypes.byref(array), ctypes.sizeof(_INPUT))
        if sent != count:
            error = _get_last_error()
            raise ComputerUseError(
                f"SendInput injected {sent}/{count} events "
                f"(error {error}); is the target window elevated (UIPI) or the "
                "session locked?"
            )

    def _mouse(
        self, flags: int, *, dx: int = 0, dy: int = 0, data: int = 0
    ) -> "_INPUT":
        event = _INPUT()
        event.type = INPUT_MOUSE
        event.u.mi.dx = dx
        event.u.mi.dy = dy
        event.u.mi.mouseData = data & 0xFFFFFFFF
        event.u.mi.dwFlags = flags
        return event

    def _key(
        self, vk: int, *, up: bool, extended: bool = False, scan: int = 0
    ) -> "_INPUT":
        event = _INPUT()
        event.type = INPUT_KEYBOARD
        event.u.ki.wVk = 0 if scan else vk
        event.u.ki.wScan = scan
        flags = KEYEVENTF_KEYUP if up else 0
        if scan:
            flags |= KEYEVENTF_UNICODE
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        event.u.ki.dwFlags = flags
        return event

    def _pointer(self, x: int, y: int) -> "_INPUT":
        """A move event for a primary-monitor pixel coordinate."""
        width, height = self._primary_screen()
        return self._mouse(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
            dx=normalize_absolute(int(x), 0, width),
            dy=normalize_absolute(int(y), 0, height),
        )

    # ── contract: capture ────────────────────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        self._ensure_open()
        return self._primary_screen()

    def capture(self, region: Region | None = None) -> RasterImage:
        self._ensure_open()
        width, height = self._primary_screen()
        if region is None:
            src_x, src_y, grab_w, grab_h = 0, 0, width, height
        else:
            src_x, src_y = region.x, region.y
            grab_w, grab_h = region.width, region.height
        # Clamp inside the primary monitor so BitBlt never reads out of bounds.
        src_x = max(0, min(src_x, width - 1))
        src_y = max(0, min(src_y, height - 1))
        grab_w = max(1, min(grab_w, width - src_x))
        grab_h = max(1, min(grab_h, height - src_y))

        data = self._grab_bgra(src_x, src_y, grab_w, grab_h)
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ComputerUseError(
                "The Windows backend needs Pillow to encode screenshots. "
                "Install with: uv sync --extra computeruse-windows"
            ) from exc
        image = Image.frombytes("RGB", (grab_w, grab_h), data, "raw", "BGRX")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return RasterImage(data=buffer.getvalue(), width=grab_w, height=grab_h)

    def _grab_bgra(self, x: int, y: int, width: int, height: int) -> bytes:
        """Capture a rectangle of the desktop as a top-down BGRX byte buffer."""
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise ComputerUseError("GetDC(NULL) failed; no interactive desktop?")
        mem_dc = None
        bitmap = None
        previous = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not mem_dc or not bitmap:
                raise ComputerUseError("Could not create GDI capture surfaces")
            previous = gdi32.SelectObject(mem_dc, bitmap)
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, x, y, SRCCOPY):
                raise ComputerUseError(f"BitBlt failed (error {_get_last_error()})")
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down rows
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = BI_RGB
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(
                mem_dc,
                bitmap,
                0,
                height,
                buffer,
                ctypes.byref(info),
                DIB_RGB_COLORS,
            ):
                raise ComputerUseError(f"GetDIBits failed (error {_get_last_error()})")
            return buffer.raw
        finally:
            if mem_dc and previous:
                gdi32.SelectObject(mem_dc, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            if screen_dc:
                user32.ReleaseDC(None, screen_dc)

    # ── contract: input ──────────────────────────────────────────────────

    def move(self, x: int, y: int) -> None:
        self._ensure_open()
        self._send(self._pointer(x, y))

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._ensure_open()
        flags = _BUTTONS.get(button)
        if flags is None:
            raise ComputerUseError(
                f"Unknown mouse button {button!r}; use left/middle/right"
            )
        if clicks < 1:
            raise ComputerUseError("clicks must be >= 1")
        down, up = flags
        self._send(self._pointer(x, y))
        for index in range(clicks):
            self._send(self._mouse(down), self._mouse(up))
            if clicks > 1 and index < clicks - 1:
                time.sleep(0.05)

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3
    ) -> None:
        self._ensure_open()
        steps = max(1, int(duration / 0.02))
        self._send(self._pointer(x1, y1), self._mouse(MOUSEEVENTF_LEFTDOWN))
        for step in range(1, steps + 1):
            ix = x1 + (x2 - x1) * step // steps
            iy = y1 + (y2 - y1) * step // steps
            self._send(self._pointer(ix, iy))
            time.sleep(duration / steps)
        self._send(self._pointer(x2, y2), self._mouse(MOUSEEVENTF_LEFTUP))

    def scroll(self, x: int, y: int, *, dx: int, dy: int) -> None:
        self._ensure_open()
        events = [self._pointer(x, y)]
        if dy:
            events.append(self._mouse(MOUSEEVENTF_WHEEL, data=wheel_amount(dy)))
        if dx:
            events.append(
                self._mouse(MOUSEEVENTF_HWHEEL, data=wheel_amount(dx, horizontal=True))
            )
        self._send(*events)

    def type_text(self, text: str) -> None:
        self._ensure_open()
        for char in text:
            encoded = char.encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index : index + 2], "little")
                self._send(
                    self._key(0, up=False, scan=unit),
                    self._key(0, up=True, scan=unit),
                )

    def press(self, keys: list[str]) -> None:
        self._ensure_open()
        if not keys:
            raise ComputerUseError("keys must not be empty")
        held: list[tuple[int, bool]] = []
        for name in keys:
            held.extend(resolve_keys(name))
        events = [self._key(vk, up=False, extended=ext) for vk, ext in held]
        events += [self._key(vk, up=True, extended=ext) for vk, ext in reversed(held)]
        self._send(*events)

    def close(self) -> None:
        self._closed = True
