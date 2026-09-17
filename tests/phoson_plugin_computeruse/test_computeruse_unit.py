"""Unit tests for the bundled Computer Use plugin (issue #223).

No display needed: every test runs against :class:`FakeBackend`, which records
actions and returns a dependency-free PNG. The permission guarantee is asserted
at the policy level (input tools must resolve to ``ask`` from their hints).
"""

import pytest

from phoson_agent.models import ImageToolResult
from phoson_agent.permissions import (
    LEVEL_ASK,
    LEVEL_ALLOW,
    PermissionPolicy,
    collect_tool_hints,
)
from phoson_plugin_computeruse import (
    Frame,
    Region,
    ComputerUsePlugin,
    fit_within,
)
from phoson_plugin_computeruse.backends import FakeBackend
from phoson_plugin_computeruse.backends.base import ComputerUseError
from phoson_plugin_computeruse.backends.factory import detect_backend

# ── geometry ─────────────────────────────────────────────────────────────


def test_fit_within_never_upscales():
    assert fit_within(320, 200, max_long_edge=1568) == (320, 200)


def test_fit_within_caps_long_edge():
    width, height = fit_within(3840, 2160, max_long_edge=1568)
    assert max(width, height) <= 1568
    assert width * height <= 1_150_000
    assert width / height == pytest.approx(3840 / 2160, rel=2e-3)


def test_fit_within_caps_total_pixels():
    width, height = fit_within(4000, 4000, max_long_edge=4000, max_pixels=1_000_000)
    assert width * height <= 1_000_000


def test_fit_within_rejects_bad_input():
    with pytest.raises(ValueError):
        fit_within(0, 10)


def test_frame_to_native_maps_and_clamps():
    frame = Frame(region=Region(100, 50, 1920, 1080), image_width=960, image_height=540)
    assert frame.to_native(0, 0) == (100, 50)
    assert frame.to_native(480, 270) == (1060, 590)
    # Out-of-range coordinates clamp inside the captured region.
    assert frame.to_native(9999, 9999) == (100 + 1919, 50 + 1079)


def test_frame_describe_mentions_coordinate_space():
    frame = Frame(region=Region(0, 0, 1920, 1080), image_width=960, image_height=540)
    text = frame.describe()
    assert "960x540" in text and "0, 959" in text and "0, 539" in text


# ── plugin helpers ───────────────────────────────────────────────────────


def _plugin(**config) -> ComputerUsePlugin:
    plugin = ComputerUsePlugin()
    merged = {"backend": "fake", "action_delay": 0.0, **config}
    plugin.configure(merged)
    plugin.initialize()
    return plugin


def _tools(plugin: ComputerUsePlugin) -> dict:
    return {tool.name: tool for tool in plugin.get_tools()}


def _backend(plugin: ComputerUsePlugin) -> FakeBackend:
    backend = plugin._backend_impl()
    assert isinstance(backend, FakeBackend)
    return backend


# ── screenshots + coordinate mapping ─────────────────────────────────────


def test_screenshot_returns_image_and_sets_frame(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path, fake_screen_size=[1280, 720])
    result = _tools(plugin)["computer_screenshot"].handler({})
    assert isinstance(result, ImageToolResult)
    assert "1280x720" in result.text
    assert result.image.media_type == "image/png"
    path = result.image.source.removeprefix("file://")
    from pathlib import Path

    assert Path(path).exists()
    assert plugin._frame == Frame(
        region=Region(0, 0, 1280, 720), image_width=1280, image_height=720
    )


def test_screenshot_downscales_when_pillow_is_available(tmp_path):
    pytest.importorskip("PIL")
    plugin = _plugin(screenshot_dir=tmp_path, max_long_edge=960)
    result = _tools(plugin)["computer_screenshot"].handler({})
    assert isinstance(result, ImageToolResult)
    assert "960x540" in result.text
    assert plugin._frame == Frame(
        region=Region(0, 0, 1920, 1080), image_width=960, image_height=540
    )


def test_click_maps_displayed_coordinates_to_native(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    # A frame as if a 1920x1080 capture had been downscaled to 960x540.
    plugin._frame = Frame(
        region=Region(0, 0, 1920, 1080), image_width=960, image_height=540
    )
    _tools(plugin)["computer_click"].handler({"x": 480, "y": 270})
    assert _backend(plugin).actions[-1] == ("click", 960, 540, "left", 1)


def test_click_without_screenshot_uses_native_coordinates(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    _tools(plugin)["computer_click"].handler({"x": 10, "y": 20})
    assert _backend(plugin).actions[-1] == ("click", 10, 20, "left", 1)


def test_region_capture_offsets_coordinates(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path, max_long_edge=1000)
    tools = _tools(plugin)
    tools["computer_screenshot"].handler(
        {"x": 100, "y": 100, "width": 200, "height": 200}
    )
    tools["computer_click"].handler({"x": 0, "y": 0})
    assert _backend(plugin).actions[-1][:3] == ("click", 100, 100)


def test_partial_region_is_rejected(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    result = _tools(plugin)["computer_screenshot"].handler({"x": 1, "y": 2})
    assert isinstance(result, str) and "x, y, width and height" in result


def test_double_click_and_drag_are_forwarded(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    tools = _tools(plugin)
    tools["computer_click"].handler({"x": 1, "y": 2, "clicks": 2})
    tools["computer_drag"].handler({"x1": 1, "y1": 2, "x2": 30, "y2": 40})
    assert _backend(plugin).actions[-2] == ("click", 1, 2, "left", 2)
    assert _backend(plugin).actions[-1][:5] == ("drag", 1, 2, 30, 40)


def test_type_and_key(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    tools = _tools(plugin)
    tools["computer_type"].handler({"text": "hello"})
    tools["computer_key"].handler({"keys": ["ctrl", "s"]})
    assert _backend(plugin).actions[-2] == ("type", "hello")
    assert _backend(plugin).actions[-1] == ("press", ("ctrl", "s"))


def test_scroll_forwards_notches(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    _tools(plugin)["computer_scroll"].handler({"x": 5, "y": 6, "dy": -3})
    assert _backend(plugin).actions[-1] == ("scroll", 5, 6, 0, -3)


def test_wait_clamps(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("phoson_plugin_computeruse._plugin.time.sleep", slept.append)
    plugin = _plugin(screenshot_dir=tmp_path)
    assert "60.0s" in _tools(plugin)["computer_wait"].handler({"seconds": 999})
    assert slept == [60.0]


def test_empty_inputs_are_rejected(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    tools = _tools(plugin)
    assert "must not be empty" in tools["computer_type"].handler({"text": ""})
    assert "must not be empty" in tools["computer_key"].handler({"keys": []})
    assert "clicks must be" in tools["computer_click"].handler(
        {"x": 0, "y": 0, "clicks": 0}
    )


def test_close_is_idempotent(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path)
    backend = _backend(plugin)
    plugin.close()
    plugin.close()
    assert backend.closed is True


def test_unknown_backend_is_actionable():
    plugin = ComputerUsePlugin()
    plugin.configure({"backend": "nope"})
    with pytest.raises(ComputerUseError, match="Unknown backend"):
        plugin.initialize()


# ── permissions (the #169/#227 vocabulary) ───────────────────────────────


def test_input_tools_do_not_prompt_by_default():
    """Computer use must not gate on confirmation (explicit product decision)."""
    tools = _plugin().get_tools()
    policy = PermissionPolicy(hints=collect_tool_hints(tools))
    for name in (
        "computer_click",
        "computer_move",
        "computer_drag",
        "computer_scroll",
        "computer_type",
        "computer_key",
    ):
        assert policy.check(name) == LEVEL_ALLOW, name


def test_require_confirmation_restores_ask():
    tools = _plugin(require_confirmation=True).get_tools()
    policy = PermissionPolicy(hints=collect_tool_hints(tools))
    for name in (
        "computer_click",
        "computer_move",
        "computer_drag",
        "computer_scroll",
        "computer_type",
        "computer_key",
    ):
        assert policy.check(name) == LEVEL_ASK, name
    # The read-only tools stay allow either way.
    assert policy.check("computer_screenshot") == LEVEL_ALLOW


def test_read_only_tools_resolve_to_allow():
    tools = _plugin().get_tools()
    policy = PermissionPolicy(hints=collect_tool_hints(tools))
    assert policy.check("computer_screenshot") == LEVEL_ALLOW
    assert policy.check("computer_wait") == LEVEL_ALLOW


# ── end-to-end (4 steps: observe → click → type → verify) ────────────────


def test_four_step_flow(tmp_path):
    plugin = _plugin(screenshot_dir=tmp_path, fake_screen_size=[1280, 720])
    tools = _tools(plugin)
    assert isinstance(tools["computer_screenshot"].handler({}), ImageToolResult)
    tools["computer_click"].handler({"x": 100, "y": 200})
    tools["computer_type"].handler({"text": "hello world"})
    assert isinstance(tools["computer_screenshot"].handler({}), ImageToolResult)
    names = [action[0] for action in _backend(plugin).actions]
    assert names == ["click", "type"]
    assert len(_backend(plugin).captures) == 2


def test_plugin_metadata():
    plugin = ComputerUsePlugin()
    assert plugin.name == "phoson-plugin-computeruse"
    assert plugin.version == "0.1.0"
    assert "desktop" in plugin.description.lower()


# ── backend detection (fail closed on Wayland) ───────────────────────────


def test_auto_uses_wayland_backend_on_gnome(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")  # XWayland is present too
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert detect_backend(name="auto").name == "wayland"


def test_auto_detects_wayland_from_session_type_on_gnome(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert detect_backend(name="auto").name == "wayland"


def test_auto_refuses_wayland_on_non_gnome(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_SESSION_DESKTOP", "KDE")
    with pytest.raises(ComputerUseError, match="GNOME only"):
        detect_backend(name="auto")


def test_auto_selects_x11_on_non_wayland(monkeypatch):
    from unittest.mock import Mock

    from phoson_plugin_computeruse.backends import x11, factory

    monkeypatch.setattr(factory.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":99")
    # Test selection, not installed extras or access to a live display.
    backend = object()
    constructor = Mock(return_value=backend)
    monkeypatch.setattr(x11, "X11Backend", constructor)
    assert detect_backend(name="auto") is backend
    constructor.assert_called_once_with()


def test_fake_backend_works_even_on_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    backend = detect_backend(name="fake", screen_size=(800, 600))
    assert backend.screen_size() == (800, 600)
