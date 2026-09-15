"""X11 backend: ``mss`` capture + XTEST input, no root required.

This is the primary supported backend (and the one exercised in CI via Xvfb).
It requires an X11 ``DISPLAY``; it does **not** work on native Wayland — use
``wayland`` support via the portal, which is deferred (see the plan).

XWayland note: XTEST only reaches XWayland clients, not native Wayland ones, so
this backend is not a Wayland solution.
"""

import time
from io import BytesIO

from .base import RasterImage, ComputerBackend, ComputerUseError
from ..geometry import Region

X11_AVAILABLE = True
_X11_IMPORT_ERROR: Exception | None = None

try:  # optional dependency, like `asyncssh` for phoson_plugin_ssh
    import mss as _mss
    import Xlib
    import Xlib.XK
    import Xlib.display
    import Xlib.ext.xtest
    from PIL import Image as _Image
except ImportError as exc:  # pragma: no cover - exercised via the flag
    X11_AVAILABLE = False
    _X11_IMPORT_ERROR = exc


#: X11 button numbers for the pointer.
_BUTTONS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}

#: Named keys the agent may pass to ``computer_key``.
_KEY_NAMES: dict[str, str] = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "shift": "Shift_L",
    "super": "Super_L",
    "win": "Super_L",
    "cmd": "Super_L",
    "meta": "Super_L",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "f1": "F1",
    "f2": "F2",
    "f3": "F3",
    "f4": "F4",
    "f5": "F5",
    "f6": "F6",
    "f7": "F7",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "f11": "F11",
    "f12": "F12",
}

#: Characters that need a keysym name rather than the glyph itself.
_CHAR_NAMES: dict[str, str] = {
    "\n": "Return",
    "\t": "Tab",
    "\r": "Return",
    " ": "space",
    "\b": "BackSpace",
}


class X11Backend(ComputerBackend):
    """Drive an X11 display through XTEST."""

    name = "x11"

    def __init__(self) -> None:
        if not X11_AVAILABLE:
            raise ComputerUseError(
                "The X11 backend needs the optional 'mss', 'Pillow' and "
                "'python-xlib' packages. Install with: "
                "uv sync --extra computeruse  (or: "
                "pip install 'phoson-engine-minimal[computeruse]'). "
                f"Original error: {_X11_IMPORT_ERROR}"
            )
        try:
            self._display = Xlib.display.Display()
        except Exception as exc:  # noqa: BLE001 - surface a clear X11 error
            raise ComputerUseError(
                f"Could not open an X11 display: {exc}. Is DISPLAY set and the "
                "X server reachable?"
            ) from exc
        self._root = self._display.screen().root
        self._closed = False

    # ── helpers ──────────────────────────────────────────────────────────

    def _ensure_open(self) -> None:
        if self._closed:
            raise ComputerUseError("X11 backend is closed")

    def _keysym(self, name: str) -> int:
        key = name.lower()
        key = _KEY_NAMES.get(key, name if len(name) == 1 else name)
        keysym = Xlib.XK.string_to_keysym(key)
        if keysym == 0 and len(name) == 1:
            keysym = Xlib.XK.string_to_keysym(_CHAR_NAMES.get(name, name))
        if keysym == 0:
            raise ComputerUseError(f"Unsupported key name: {name!r}")
        return keysym

    def _keycode(self, name: str) -> tuple[int, bool]:
        """Return ``(keycode, needs_shift)`` for a key name or single char."""
        if len(name) == 1 and name in _CHAR_NAMES:
            name = _CHAR_NAMES[name]
        keysym = self._keysym(name)
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise ComputerUseError(f"No keycode for key {name!r}")
        unshifted = self._display.keycode_to_keysym(keycode, 0)
        shifted = self._display.keycode_to_keysym(keycode, 1)
        if unshifted == keysym:
            return keycode, False
        if shifted == keysym:
            return keycode, True
        return keycode, False

    def _tap(self, keycode: int) -> None:
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.KeyPress, keycode)
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.KeyRelease, keycode)

    def _press_shift(self) -> int:
        shift, _ = self._keycode("shift")
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.KeyPress, shift)
        return shift

    def _release_shift(self, shift: int) -> None:
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.KeyRelease, shift)

    # ── contract ─────────────────────────────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        self._ensure_open()
        geom = self._root.get_geometry()
        return int(geom.width), int(geom.height)

    def capture(self, region: Region | None = None) -> RasterImage:
        self._ensure_open()
        if region is None:
            w, h = self.screen_size()
            box: dict[str, int] = {"left": 0, "top": 0, "width": w, "height": h}
        else:
            box = {
                "left": region.x,
                "top": region.y,
                "width": region.width,
                "height": region.height,
            }
        with _mss.mss() as sct:
            shot = sct.grab(box)
        image = _Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return RasterImage(data=buffer.getvalue(), width=shot.width, height=shot.height)

    def move(self, x: int, y: int) -> None:
        self._ensure_open()
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.MotionNotify, x=x, y=y)
        self._display.sync()

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._ensure_open()
        number = _BUTTONS.get(button)
        if number is None:
            raise ComputerUseError(
                f"Unknown mouse button {button!r}; use left/middle/right"
            )
        if clicks < 1:
            raise ComputerUseError("clicks must be >= 1")
        self.move(x, y)
        for _ in range(clicks):
            Xlib.ext.xtest.fake_input(self._display, Xlib.X.ButtonPress, number)
            Xlib.ext.xtest.fake_input(self._display, Xlib.X.ButtonRelease, number)
            self._display.sync()
            if clicks > 1:
                time.sleep(0.05)

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3
    ) -> None:
        self._ensure_open()
        steps = max(1, int(duration / 0.02))
        self.move(x1, y1)
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.ButtonPress, 1)
        self._display.sync()
        for step in range(1, steps + 1):
            ix = x1 + (x2 - x1) * step // steps
            iy = y1 + (y2 - y1) * step // steps
            Xlib.ext.xtest.fake_input(self._display, Xlib.X.MotionNotify, x=ix, y=iy)
            self._display.sync()
            time.sleep(duration / steps)
        Xlib.ext.xtest.fake_input(self._display, Xlib.X.ButtonRelease, 1)
        self._display.sync()

    def scroll(self, x: int, y: int, *, dx: int, dy: int) -> None:
        self._ensure_open()
        self.move(x, y)
        # X11 sends discrete wheel events: 4/5 vertical, 6/7 horizontal.
        events = [(dy, 5, 4), (dx, 7, 6)]  # (amount, positive button, negative)
        for amount, positive, negative in events:
            button = positive if amount > 0 else negative
            for _ in range(abs(int(amount))):
                Xlib.ext.xtest.fake_input(self._display, Xlib.X.ButtonPress, button)
                Xlib.ext.xtest.fake_input(self._display, Xlib.X.ButtonRelease, button)
        self._display.sync()

    def type_text(self, text: str) -> None:
        self._ensure_open()
        for char in text:
            keycode, shift = self._keycode(char)
            shift_code = self._press_shift() if shift else 0
            try:
                self._tap(keycode)
            finally:
                if shift:
                    self._release_shift(shift_code)
        self._display.sync()

    def press(self, keys: list[str]) -> None:
        self._ensure_open()
        if not keys:
            raise ComputerUseError("keys must not be empty")
        pressed: list[int] = []
        try:
            for name in keys:
                keycode, shift = self._keycode(name)
                if shift:
                    shift_code = self._press_shift()
                    pressed.append(shift_code)
                Xlib.ext.xtest.fake_input(self._display, Xlib.X.KeyPress, keycode)
                pressed.append(keycode)
        finally:
            for keycode in reversed(pressed):
                Xlib.ext.xtest.fake_input(self._display, Xlib.X.KeyRelease, keycode)
            self._display.sync()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._display.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    """Documents that ``Any`` is imported for typing of optional handles."""
