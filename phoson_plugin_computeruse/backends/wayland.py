"""Wayland backend (GNOME): compositor D-Bus APIs, no libei required.

Wayland gives ordinary clients no way to capture the screen or inject input;
the sanctioned routes are the xdg portals and compositor-specific APIs. On
GNOME, Mutter exposes both over the session bus in a *synchronous* form, which
makes them far easier to drive than the portal's async Request/Response dance:

- capture: the xdg **Screenshot portal** (``org.freedesktop.portal.Screenshot``,
  ``interactive=false``). GNOME 49 denies ``org.gnome.Shell.Screenshot`` to
  ordinary clients (``AccessDenied``), so the shell API is only a fallback.
- input: ``org.gnome.Mutter.RemoteDesktop.Session`` — ``NotifyPointerButton``,
  ``NotifyPointerAxisDiscrete``, ``NotifyKeyboardKeysym`` and
  ``NotifyPointerMotionRelative``. Signatures verified against Mutter 49.

Both are driven through :mod:`phoson_plugin_computeruse.backends.dbus` using
**jeepney** (a persistent connection is required: the compositor destroys a
session when its creating D-Bus client disconnects).

Honest limitations (see the plugin README):

- GNOME only. Other compositors need the portal + libei path.
- **Pointer motion is relative-only.** ``NotifyPointerMotionAbsolute`` needs a
  ScreenCast stream that the *portal* links to the remote-desktop session;
  Mutter 49 has no API to link one, so absolute coordinates are reconstructed
  from relative deltas off a known origin (see :meth:`_home_pointer`).
- ``Start()`` shows the compositor's remote-control indicator and requires the
  user's session to allow it.
"""

import logging
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

from .base import RasterImage, ComputerBackend, ComputerUseError
from .dbus import (
    DBusError,
    DBusTransport,
    JeepneyTransport,
    all_quoted,
    starts_true,
    first_object_path,
)
from ..geometry import Region

logger = logging.getLogger(__name__)

_IFACE_RD = "org.gnome.Mutter.RemoteDesktop"
_PATH_RD = "/org/gnome/Mutter/RemoteDesktop"
_IFACE_RD_SESSION = "org.gnome.Mutter.RemoteDesktop.Session"


_IFACE_SHOT = "org.gnome.Shell.Screenshot"
_PATH_SHOT = "/org/gnome/Shell/Screenshot"

#: evdev button codes expected by ``NotifyPointerButton``.
_BUTTONS = {"left": 0x110, "middle": 0x112, "right": 0x111}

#: ``NotifyPointerAxisDiscrete`` axis ids.
_AXIS_VERTICAL = 0
_AXIS_HORIZONTAL = 1

#: X11 keysyms for non-printable keys (ASCII keysyms equal their code point).
_KEY_SYMS = {
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    "shift": 0xFFE1,
    "super": 0xFFEB,
    "win": 0xFFEB,
    "cmd": 0xFFEB,
    "meta": 0xFFEB,
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "tab": 0xFF09,
    "space": 0x20,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50,
    "end": 0xFF57,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "up": 0xFF52,
    "down": 0xFF54,
    "left": 0xFF51,
    "right": 0xFF53,
    "f1": 0xFFBE,
    "f2": 0xFFBF,
    "f3": 0xFFC0,
    "f4": 0xFFC1,
    "f5": 0xFFC2,
    "f6": 0xFFC3,
    "f7": 0xFFC4,
    "f8": 0xFFC5,
    "f9": 0xFFC6,
    "f10": 0xFFC7,
    "f11": 0xFFC8,
    "f12": 0xFFC9,
}

#: Characters that map to a named keysym rather than their code point.
_CHAR_SYMS = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08}


def _crop_png(data: bytes, region: Region) -> bytes:
    """Crop a PNG to ``region`` (needs Pillow).

    The xdg Screenshot portal has no region option, so a region capture is a
    full-screen shot cropped afterwards.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on the env
        raise ComputerUseError(
            "Region capture on Wayland needs Pillow (the portal only captures "
            "the whole screen). Install with: uv sync --extra computeruse"
        ) from exc
    image = Image.open(BytesIO(data)).crop(
        (region.x, region.y, region.right, region.bottom)
    )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_size(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG's IHDR — no Pillow needed."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ComputerUseError("capture did not return a PNG")
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


class WaylandBackend(ComputerBackend):
    """Capture and input on GNOME Wayland via Mutter's D-Bus APIs."""

    name = "wayland"

    def __init__(self, transport: DBusTransport | None = None) -> None:
        # The transport is created lazily: selecting the backend must not
        # require `jeepney`, only actually driving the compositor does.
        self._transport = transport
        self._rd_session: str | None = None
        self._size: tuple[int, int] | None = None
        self._pos: tuple[int, int] = (0, 0)
        self._closed = False

    def _bus(self) -> DBusTransport:
        """The session-bus transport, opened on first use.

        A persistent connection is required: the compositor destroys a session
        when its creating D-Bus client disconnects, so the one-shot `gdbus`
        transport cannot hold one.
        """
        if self._transport is None:
            self._transport = JeepneyTransport()
        return self._transport

    # ── helpers ──────────────────────────────────────────────────────────

    def _ensure_open(self) -> None:
        if self._closed:
            raise ComputerUseError("Wayland backend is closed")

    def _session(self) -> str:
        """RemoteDesktop session path, created and started on first use."""
        if self._rd_session is not None:
            return self._rd_session
        try:
            reply = self._bus().call(_IFACE_RD, _PATH_RD, _IFACE_RD, "CreateSession")
            path = first_object_path(reply)
            if not path:
                raise ComputerUseError("CreateSession returned no session path")
            self._bus().call(_IFACE_RD, path, _IFACE_RD_SESSION, "Start")
        except DBusError as exc:
            raise ComputerUseError(
                "Could not start a GNOME remote-desktop session: "
                f"{exc}. On GNOME this shows a remote-control indicator; make "
                "sure the session allows it."
            ) from exc
        self._rd_session = path
        return path

    # ── contract ─────────────────────────────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        if self._size is None:
            self._size = _png_size(self.capture().data)
        return self._size

    def capture(self, region: Region | None = None) -> RasterImage:
        self._ensure_open()
        data = self._capture_portal()
        if data is None:
            # Fallback for setups without a desktop portal: GNOME Shell's own
            # API. GNOME 49 denies it to arbitrary clients (AccessDenied), so
            # the portal above is the working path there.
            data = self._capture_shell(region)
            width, height = (
                _png_size(data) if region is None else (region.width, region.height)
            )
            return RasterImage(data=data, width=width, height=height)
        width, height = _png_size(data)
        if region is not None:
            data = _crop_png(data, region)
            width, height = region.width, region.height
        return RasterImage(data=data, width=width, height=height)

    def _capture_portal(self) -> bytes | None:
        """Capture through ``org.freedesktop.portal.Screenshot``.

        This is the Wayland-sanctioned path (`interactive=false`), and the one
        that works on GNOME 49. Returns ``None`` when the portal is
        unavailable so the caller can fall back.
        """
        try:
            results = self._bus().request(
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                "sa{sv}",
                ("", {"interactive": ("b", False), "handle_token": ("s", "phoson")}),
            )
        except DBusError as exc:
            logger.debug("portal screenshot unavailable: %s", exc)
            return None
        uri = results.get("uri")
        if not isinstance(uri, str):
            return None
        path = Path(unquote(urlparse(uri).path))
        try:
            data = path.read_bytes()
        finally:
            # The portal writes a new file per request purely to hand it back;
            # remove it so an agent loop does not litter the Screenshots dir.
            path.unlink(missing_ok=True)
        return data

    def _capture_shell(self, region: Region | None) -> bytes:
        """Capture via ``org.gnome.Shell.Screenshot`` (older GNOME only)."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            path = Path(handle.name)
        try:
            if region is None:
                reply = self._bus().call(
                    _IFACE_SHOT,
                    _PATH_SHOT,
                    _IFACE_SHOT,
                    "Screenshot",
                    False,
                    False,
                    str(path),
                )
            else:
                reply = self._bus().call(
                    _IFACE_SHOT,
                    _PATH_SHOT,
                    _IFACE_SHOT,
                    "ScreenshotArea",
                    region.x,
                    region.y,
                    region.width,
                    region.height,
                    False,
                    str(path),
                )
            if not starts_true(reply):
                raise ComputerUseError(f"GNOME screenshot refused: {reply}")
            used = all_quoted(reply)
            written = Path(used[-1]) if used else path
            data = written.read_bytes()
        except DBusError as exc:
            raise ComputerUseError(
                "Screenshot failed: the desktop portal is unavailable and "
                f"GNOME Shell refused its own screenshot API ({exc})."
            ) from exc
        finally:
            path.unlink(missing_ok=True)
        return data

    def _home_pointer(self, session: str) -> None:
        """Park the cursor at a known position using relative motion.

        The direct RemoteDesktop API cannot do absolute motion on GNOME: the
        ``stream`` argument of ``NotifyPointerMotionAbsolute`` must belong to a
        ScreenCast session that the *portal* links internally, and Mutter 49
        exposes no way to link one (calls fail with *"No screen cast active"*).

        Absolute coordinates are therefore built from relative deltas off a
        known origin. Mutter clamps pointer motion at the screen edges, so
        overshooting to the bottom-right parks the cursor exactly at
        ``(width-1, height-1)``. The bottom-right is used because GNOME's
        Activities hot corner is the *top-left*.
        """
        width, height = self.screen_size()
        self._bus().call(
            _IFACE_RD,
            session,
            _IFACE_RD_SESSION,
            "NotifyPointerMotionRelative",
            float(width * 4),
            float(height * 4),
        )
        self._pos = (width - 1, height - 1)

    def _glide(self, x: int, y: int, session: str) -> None:
        """Move relative to the tracked position, without re-homing."""
        width, height = self.screen_size()
        tx = min(max(int(x), 0), width - 1)
        ty = min(max(int(y), 0), height - 1)
        dx, dy = tx - self._pos[0], ty - self._pos[1]
        if dx or dy:
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyPointerMotionRelative",
                float(dx),
                float(dy),
            )
        self._pos = (tx, ty)

    def move(self, x: int, y: int) -> None:
        self._ensure_open()
        session = self._session()
        # Re-home every time: the user may have moved the pointer since the
        # last tracked position, and accuracy beats the extra D-Bus round-trip.
        self._home_pointer(session)
        self._glide(x, y, session)

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._ensure_open()
        code = _BUTTONS.get(button)
        if code is None:
            raise ComputerUseError(f"Unknown mouse button {button!r}")
        if clicks < 1:
            raise ComputerUseError("clicks must be >= 1")
        session = self._session()
        self.move(x, y)
        for _ in range(clicks):
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyPointerButton",
                code,
                True,
            )
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyPointerButton",
                code,
                False,
            )

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3
    ) -> None:
        self._ensure_open()
        session = self._session()
        # One home for the whole gesture, then glide without re-homing so the
        # button stays down at the right place.
        self._home_pointer(session)
        self._glide(x1, y1, session)
        self._bus().call(
            _IFACE_RD, session, _IFACE_RD_SESSION, "NotifyPointerButton", 0x110, True
        )
        steps = max(1, int(duration / 0.05))
        for step in range(1, steps + 1):
            self._glide(
                x1 + (x2 - x1) * step // steps,
                y1 + (y2 - y1) * step // steps,
                session,
            )
        self._bus().call(
            _IFACE_RD, session, _IFACE_RD_SESSION, "NotifyPointerButton", 0x110, False
        )

    def scroll(self, x: int, y: int, *, dx: int, dy: int) -> None:
        self._ensure_open()
        session = self._session()
        self.move(x, y)
        if dy:
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyPointerAxisDiscrete",
                _AXIS_VERTICAL,
                int(dy),
            )
        if dx:
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyPointerAxisDiscrete",
                _AXIS_HORIZONTAL,
                int(dx),
            )

    def _tap_keysym(self, session: str, keysym: int) -> None:
        self._bus().call(
            _IFACE_RD,
            session,
            _IFACE_RD_SESSION,
            "NotifyKeyboardKeysym",
            keysym,
            True,
        )
        self._bus().call(
            _IFACE_RD,
            session,
            _IFACE_RD_SESSION,
            "NotifyKeyboardKeysym",
            keysym,
            False,
        )

    def type_text(self, text: str) -> None:
        self._ensure_open()
        session = self._session()
        for char in text:
            keysym = _CHAR_SYMS.get(char)
            if keysym is None:
                if not (0x20 <= ord(char) <= 0x7E):
                    raise ComputerUseError(
                        f"Cannot type {char!r}: only printable ASCII is supported"
                    )
                keysym = ord(char)
            self._tap_keysym(session, keysym)

    def press(self, keys: list[str]) -> None:
        self._ensure_open()
        if not keys:
            raise ComputerUseError("keys must not be empty")
        session = self._session()
        keysyms: list[int] = []
        for name in keys:
            keysym = _KEY_SYMS.get(name.lower())
            if keysym is None and len(name) == 1:
                keysym = ord(name)
            if keysym is None:
                raise ComputerUseError(f"Unsupported key name: {name!r}")
            keysyms.append(keysym)
        for keysym in keysyms:
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyKeyboardKeysym",
                keysym,
                True,
            )
        for keysym in reversed(keysyms):
            self._bus().call(
                _IFACE_RD,
                session,
                _IFACE_RD_SESSION,
                "NotifyKeyboardKeysym",
                keysym,
                False,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for iface, path, session_iface in (
            (_IFACE_RD, self._rd_session, _IFACE_RD_SESSION),
        ):
            if not path:
                continue
            try:
                self._bus().call(iface, path, session_iface, "Stop")
            except DBusError:
                pass  # "Session not started" is expected for unstarted sessions
        self._rd_session = None
        closer = getattr(self._transport, "close", None)
        if closer is not None:
            closer()
