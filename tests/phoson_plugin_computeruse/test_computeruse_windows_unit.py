"""Tests for the Windows Computer Use backend (issue #251).

Two layers:

- **Portable** tests (run on the Linux CI runner) exercise the pure helpers —
  coordinate normalization, wheel conversion, key resolution — plus backend
  selection, with the ``WindowsBackend`` constructor mocked so nothing touches
  ``user32``.
- **Windows-only** smoke tests (``skipif``) drive the real GDI/``SendInput``
  path on the ``windows-latest`` runner.
"""

import sys

import pytest

from phoson_plugin_computeruse.backends import factory, windows
from phoson_plugin_computeruse.backends.base import ComputerUseError
from phoson_plugin_computeruse.backends.factory import detect_backend
from phoson_plugin_computeruse.backends.windows import (
    WHEEL_DELTA,
    virtual_key,
    resolve_keys,
    wheel_amount,
    normalize_absolute,
)

# ── coordinate normalization ─────────────────────────────────────────────


def test_normalize_absolute_maps_corners():
    assert normalize_absolute(0, 0, 1920) == 0
    assert normalize_absolute(1919, 0, 1920) == 65535
    assert normalize_absolute(960, 0, 1920) == round(960 * 65535 / 1919)


def test_normalize_absolute_respects_negative_origin():
    # The helper accepts an arbitrary origin; the Windows backend always passes
    # 0 (primary-monitor space), but a non-zero origin must not break the math.
    assert normalize_absolute(-1920, -1920, 3840) == 0
    assert normalize_absolute(0, -1920, 3840) == round(1920 * 65535 / 3839)
    assert normalize_absolute(1919, -1920, 3840) == 65535


def test_normalize_absolute_clamps_and_handles_edges():
    assert normalize_absolute(-50, 0, 1920) == 0
    assert normalize_absolute(99999, 0, 1920) == 65535
    assert normalize_absolute(10, 0, 1) == 0  # degenerate extent


# ── wheel conversion ─────────────────────────────────────────────────────


def test_wheel_amount_flips_vertical_sign():
    # Contract: positive dy scrolls down; mouseData is positive scrolling up.
    assert wheel_amount(3) == -3 * WHEEL_DELTA
    assert wheel_amount(-1) == WHEEL_DELTA
    assert wheel_amount(0) == 0


def test_wheel_amount_horizontal_is_unflipped():
    assert wheel_amount(2, horizontal=True) == 2 * WHEEL_DELTA


# ── key resolution ───────────────────────────────────────────────────────


def test_virtual_key_named_keys():
    assert virtual_key("enter") == virtual_key("return") == 0x0D
    assert virtual_key("CTRL") == 0x11  # case-insensitive
    assert virtual_key("f12") == 0x7B
    assert virtual_key("a") == 0x41
    assert virtual_key("0") == 0x30
    assert virtual_key("nope") is None


def test_resolve_keys_marks_extended_keys():
    assert resolve_keys("left") == [(0x25, True)]
    assert resolve_keys("enter") == [(0x0D, False)]
    assert resolve_keys("shift") == [(0x10, False)]


def test_resolve_keys_rejects_unknown():
    with pytest.raises(ComputerUseError, match="Unsupported key name"):
        resolve_keys("not-a-key")


# ── backend selection ────────────────────────────────────────────────────


def test_auto_selects_windows_on_win32(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(factory.sys, "platform", "win32")
    backend = object()
    constructor = Mock(return_value=backend)
    monkeypatch.setattr(windows, "WindowsBackend", constructor)
    assert detect_backend(name="auto") is backend
    constructor.assert_called_once_with()


def test_explicit_windows_backend_is_selected(monkeypatch):
    from unittest.mock import Mock

    backend = object()
    constructor = Mock(return_value=backend)
    monkeypatch.setattr(windows, "WindowsBackend", constructor)
    assert detect_backend(name="windows") is backend


def test_unknown_backend_lists_windows():
    with pytest.raises(ComputerUseError, match="windows"):
        detect_backend(name="nope")


def test_windows_backend_refuses_other_platforms():
    if sys.platform == "win32":
        pytest.skip("this assertion only holds off Windows")
    with pytest.raises(ComputerUseError, match="only runs on Windows"):
        windows.WindowsBackend()


# ── real GDI / SendInput smoke (Windows runner only) ─────────────────────

_requires_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows backend runs only on Windows"
)


@_requires_windows
def test_windows_screen_size_is_positive():
    backend = windows.WindowsBackend()
    try:
        width, height = backend.screen_size()
        assert width > 0 and height > 0
    finally:
        backend.close()


@_requires_windows
def test_windows_capture_returns_png():
    backend = windows.WindowsBackend()
    try:
        raster = backend.capture()
        assert raster.data[:8] == b"\x89PNG\r\n\x1a\n"
        assert raster.width > 0 and raster.height > 0
    finally:
        backend.close()


@_requires_windows
def test_windows_move_updates_cursor():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    backend = windows.WindowsBackend()
    try:
        target = (10, 10)
        backend.move(*target)
        point = wintypes.POINT()
        assert user32.GetCursorPos(ctypes.byref(point))
        # The primary monitor is the virtual-screen origin, so coordinates map
        # straight through.
        assert abs(point.x - target[0]) <= 2
        assert abs(point.y - target[1]) <= 2
    finally:
        backend.close()
