"""Unit tests for the GNOME Wayland backend (issue #223).

The compositor is not touched: a fake :class:`DBusTransport` records every call
and answers with canned replies, so the session lifecycle, the exact D-Bus
methods/arguments and the coordinate handling are all asserted offline.

The signatures asserted here were captured from a live GNOME 49 session via
``gdbus introspect`` — see the plugin plan.
"""

import pathlib

import pytest

from phoson_plugin_computeruse.backends import wayland as wl
from phoson_plugin_computeruse.geometry import Region
from phoson_plugin_computeruse.backends.base import ComputerUseError
from phoson_plugin_computeruse.backends.dbus import DBusError, DBusTransport
from phoson_plugin_computeruse.backends.fake import _png


class FakeTransport(DBusTransport):
    """Records D-Bus calls and emulates the GNOME services."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.fail_on = fail_on or set()

    def call(self, dest, path, interface, method, *args):
        self.calls.append((interface, method, args))
        if method in self.fail_on:
            raise DBusError(f"boom: {method}")
        if helper := getattr(self, f"_r_{method}", None):
            return helper(interface, args)
        return "()"

    # canned replies ──────────────────────────────────────────────────────
    def _r_CreateSession(self, interface, args):
        if interface == wl._IFACE_RD:
            return "(objectpath '/rd/session/1',)"
        return "(objectpath '/sc/session/1',)"

    def _r_RecordArea(self, interface, args):
        return "(objectpath '/sc/session/1/stream/1',)"

    def _r_Screenshot(self, interface, args):
        filename = args[-1]
        with open(filename, "wb") as handle:
            handle.write(_png(128, 96))
        return f"(true, '{filename}')"

    def _r_ScreenshotArea(self, interface, args):
        _x, _y, width, height, _flash, filename = args
        with open(filename, "wb") as handle:
            handle.write(_png(width, height))
        return f"(true, '{filename}')"

    def request(self, interface, method, signature, body, *, timeout=40.0):
        """Emulate a portal call that returns a file URI via a Response signal."""
        self.calls.append((interface, method, body))
        if "portal" in self.fail_on:
            raise DBusError("portal unavailable")
        import tempfile

        path = pathlib.Path(tempfile.mkdtemp()) / "shot.png"
        path.write_bytes(_png(128, 96))
        return {"uri": path.as_uri()}

    # helpers ─────────────────────────────────────────────────────────────
    def methods(self) -> list[str]:
        return [method for _iface, method, _args in self.calls]

    def interfaces(self) -> list[str]:
        return [iface for iface, _method, _args in self.calls]

    def calls_to(self, method: str) -> list[tuple]:
        return [args for _iface, name, args in self.calls if name == method]


def _backend(**kwargs) -> tuple[wl.WaylandBackend, FakeTransport]:
    transport = FakeTransport(**kwargs)
    return wl.WaylandBackend(transport=transport), transport


# ── capture ──────────────────────────────────────────────────────────────


def test_capture_returns_png_and_real_size():
    backend, transport = _backend()
    image = backend.capture()
    assert (image.width, image.height) == (128, 96)
    assert image.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert "Screenshot" in transport.methods()


def test_screen_size_comes_from_the_capture():
    backend, _ = _backend()
    assert backend.screen_size() == (128, 96)


def test_capture_uses_the_portal_by_default():
    backend, transport = _backend()
    backend.capture()
    assert "org.freedesktop.portal.Screenshot" in transport.interfaces()


def test_region_capture_crops_the_portal_shot():
    pytest.importorskip("PIL")  # cropping needs Pillow
    backend, transport = _backend()
    image = backend.capture(Region(10, 20, 30, 40))
    assert (image.width, image.height) == (30, 40)
    assert "ScreenshotArea" not in transport.methods()


def test_shell_api_is_the_fallback_when_the_portal_is_missing():
    backend, transport = _backend(fail_on={"portal"})
    image = backend.capture(Region(10, 20, 30, 40))
    assert (image.width, image.height) == (30, 40)
    assert transport.calls_to("ScreenshotArea")[0][:4] == (10, 20, 30, 40)


# ── session lifecycle ────────────────────────────────────────────────────


def test_click_creates_and_starts_a_session():
    backend, transport = _backend()
    backend.click(5, 6)
    assert transport.methods().count("CreateSession") == 1
    assert transport.methods().count("Start") == 1
    # No standalone ScreenCast session: Mutter 49 cannot link one, so absolute
    # motion is impossible and the backend does not try.
    assert "RecordArea" not in transport.methods()
    assert transport.calls_to("NotifyPointerButton") == [(0x110, True), (0x110, False)]


def test_absolute_motion_homes_then_glides_relative():
    backend, transport = _backend()
    backend.click(5, 6)
    assert "NotifyPointerMotionAbsolute" not in transport.methods()
    moves = transport.calls_to("NotifyPointerMotionRelative")
    # First the overshoot that parks the cursor at the bottom-right corner
    # (screen is 128x96 in the fake backend), then the delta to the target.
    assert moves[0] == (512.0, 384.0)
    assert moves[-1] == (5 - 127, 6 - 95)


def test_drag_homes_once_for_the_whole_gesture():
    backend, transport = _backend()
    backend.drag(10, 10, 40, 40)
    homes = [
        move
        for move in transport.calls_to("NotifyPointerMotionRelative")
        if move == (512.0, 384.0)
    ]
    assert len(homes) == 1


def test_targets_are_clamped_to_the_screen():
    backend, transport = _backend()
    backend.move(9999, -50)
    assert transport.calls_to("NotifyPointerMotionRelative")[-1] == (127 - 127, 0 - 95)


def test_click_presses_and_releases_the_button():
    backend, transport = _backend()
    backend.click(1, 2, button="right", clicks=2)
    states = transport.calls_to("NotifyPointerButton")
    # Two down/up pairs, evdev BTN_RIGHT == 0x111.
    assert states == [(0x111, True), (0x111, False), (0x111, True), (0x111, False)]


def test_close_stops_the_session_and_is_idempotent():
    backend, transport = _backend()
    backend.click(1, 2)
    backend.close()
    assert len(transport.calls_to("Stop")) == 1
    backend.close()  # idempotent
    assert len(transport.calls_to("Stop")) == 1


def test_use_after_close_fails():
    backend, _ = _backend()
    backend.close()
    with pytest.raises(ComputerUseError, match="closed"):
        backend.capture()


# ── input mapping ────────────────────────────────────────────────────────


def test_type_text_sends_ascii_keysyms():
    backend, transport = _backend()
    backend.type_text("hi")
    assert transport.calls_to("NotifyKeyboardKeysym") == [
        (ord("h"), True),
        (ord("h"), False),
        (ord("i"), True),
        (ord("i"), False),
    ]


def test_type_text_maps_newline_to_return():
    backend, transport = _backend()
    backend.type_text("\n")
    assert transport.calls_to("NotifyKeyboardKeysym")[0] == (0xFF0D, True)


def test_type_text_rejects_non_ascii():
    backend, _ = _backend()
    with pytest.raises(ComputerUseError, match="printable ASCII"):
        backend.type_text("ñ")


def test_press_releases_modifiers_in_reverse():
    backend, transport = _backend()
    backend.press(["ctrl", "s"])
    assert transport.calls_to("NotifyKeyboardKeysym") == [
        (0xFFE3, True),
        (ord("s"), True),
        (ord("s"), False),
        (0xFFE3, False),
    ]


def test_scroll_uses_discrete_axis():
    backend, transport = _backend()
    backend.scroll(3, 4, dx=1, dy=-2)
    axes = transport.calls_to("NotifyPointerAxisDiscrete")
    assert (0, -2) in axes and (1, 1) in axes


def test_close_ignores_stop_on_unstarted_sessions():
    # Live GNOME replies "Session not started" when Stop() is called before
    # Start(); teardown must swallow that rather than surface an error.
    backend, _ = _backend(fail_on={"Stop"})
    backend.click(1, 2)
    backend.close()  # must not raise


def test_unknown_button_is_rejected():
    backend, _ = _backend()
    with pytest.raises(ComputerUseError, match="Unknown mouse button"):
        backend.click(0, 0, button="thumb")


def test_unknown_key_is_rejected():
    backend, _ = _backend()
    with pytest.raises(ComputerUseError, match="Unsupported key name"):
        backend.press(["definitely-not-a-key"])
