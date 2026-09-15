"""Computer Use plugin: screenshot → decide → input over the local desktop.

Design (see ``docs/superpowers/plans/2026-09-14-computer-use-plugin.md`` and
``docs/research/computer-use-sota.md``):

- **Deterministic executor.** The plugin exposes low-level primitives; the
  engine's model plans. No hidden model orchestration lives here.
- **Coordinate mapping is owned here.** ``computer_screenshot`` stores a
  :class:`~phoson_plugin_computeruse.geometry.Frame` and every input tool maps
  *displayed-image* coordinates to native pixels, so DPI/Retina/fractional
  scaling never reaches the model. If no frame exists, coordinates are treated
  as native pixels and the result says so.
- **Safety.** Every input tool publishes destructive/open-world risk hints
  (issue #144/#227 vocabulary) so an unconfigured tool resolves to ``ask`` and
  fails closed in one-shot mode; ``computer_screenshot``/``computer_wait`` are
  read-only. Same pattern as ``phoson_plugin_ssh`` (#169).
"""

import time
import logging
from io import BytesIO
from typing import Any
from pathlib import Path

from phoson_agent.tool import tool
from phoson_llm.schemas import ImageBlock
from phoson_agent.models import AgentTool, ImageToolResult
from phoson_agent.plugin import Plugin

from .geometry import Frame, Region, fit_within
from .backends.base import ComputerBackend, ComputerUseError
from .backends.factory import detect_backend

logger = logging.getLogger(__name__)

_DEFAULT_MAX_LONG_EDGE = 1568
_DEFAULT_MAX_PIXELS = 1_150_000
_DEFAULT_ACTION_DELAY = 0.4

_SCREENSHOT_PREFIX = "computer-screenshot"

#: Risk hints published on every tool (issue #144 / #227 vocabulary).
_READ_ONLY_HINT: dict[str, Any] = {
    "mcp_annotations": {
        "annotated": True,
        "read_only": True,
        "destructive": False,
        "idempotent": True,
        "open_world": True,
    }
}
_MUTATING_HINT: dict[str, Any] = {
    "mcp_annotations": {
        "annotated": True,
        "read_only": False,
        "destructive": True,
        "idempotent": False,
        "open_world": True,
    }
}


class ComputerUsePlugin(Plugin):
    """Control the local desktop: screenshots plus mouse and keyboard input.

    Configuration (via ``configure`` / the plugin config dict):

    - ``backend``: ``auto`` (default), ``x11``, ``wayland``, ``macos`` or
      ``fake``.
    - ``require_confirmation``: when true, input tools publish destructive
      risk hints so the permission gate resolves them to ``ask`` (and fails
      closed in one-shot mode). **Default false: computer-use tools do not
      prompt.**
    - ``max_long_edge``: cap on the long edge of screenshots sent to the model
      (default 1568 px). Screenshots above the model's image limits are
      silently downscaled by the provider, which is the documented cause of
      mis-clicks; downscaling here keeps the model's view and our coordinate
      space identical.
    - ``max_pixels``: total pixel budget for a screenshot (default 1.15 MP).
    - ``action_delay``: seconds to wait after an input action so the UI can
      settle before the next screenshot (default 0.4).
    - ``screenshot_dir``: where screenshot PNGs are written (default
      ``<tmp>/phoson-computeruse``). Only the latest frames are kept.
    - ``fake_screen_size``: ``[w, h]`` used by the ``fake`` backend.
    """

    def __init__(self) -> None:
        self._backend_name = "auto"
        self._require_confirmation = False
        self._max_long_edge = _DEFAULT_MAX_LONG_EDGE
        self._max_pixels = _DEFAULT_MAX_PIXELS
        self._action_delay = _DEFAULT_ACTION_DELAY
        self._screenshot_dir: Path | None = None
        self._fake_screen_size = (1920, 1080)

        self._backend: ComputerBackend | None = None
        self._frame: Frame | None = None
        self._shot_index = 0

    # ── Plugin contract ───────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "phoson-plugin-computeruse"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Control the local desktop: screenshots, mouse and keyboard"

    def configure(self, config: dict[str, Any]) -> None:
        if "backend" in config:
            self._backend_name = str(config["backend"] or "auto")
        if "require_confirmation" in config:
            self._require_confirmation = bool(config["require_confirmation"])
        if "max_long_edge" in config:
            self._max_long_edge = max(64, int(config["max_long_edge"]))
        if "max_pixels" in config:
            self._max_pixels = max(4096, int(config["max_pixels"]))
        if "action_delay" in config:
            self._action_delay = max(0.0, float(config["action_delay"]))
        if "screenshot_dir" in config and config["screenshot_dir"]:
            self._screenshot_dir = Path(str(config["screenshot_dir"])).expanduser()
        if "fake_screen_size" in config:
            size = config["fake_screen_size"]
            if isinstance(size, (list, tuple)) and len(size) == 2:
                self._fake_screen_size = (int(size[0]), int(size[1]))

    def initialize(self) -> None:
        """Resolve the backend eagerly so misconfiguration fails fast."""
        self._backend_impl()

    def cleanup(self) -> None:
        self.close()

    def close(self) -> None:
        """Release the backend and drop the last frame. Safe to call twice."""
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("Failed to close the Computer Use backend", exc_info=True)
            self._backend = None
        self._frame = None

    # ── Backend / frame helpers ───────────────────────────────────────────

    def _backend_impl(self) -> ComputerBackend:
        if self._backend is None:
            self._backend = detect_backend(
                name=self._backend_name,
                screen_size=self._fake_screen_size,
            )
        return self._backend

    def _to_native(self, x: int, y: int) -> tuple[int, int]:
        """Map displayed-image coordinates to native, clamped to the capture."""
        frame = self._frame
        if frame is not None:
            return frame.to_native(x, y)
        width, height = self._backend_impl().screen_size()
        return min(max(int(x), 0), width - 1), min(max(int(y), 0), height - 1)

    def _settle(self) -> None:
        if self._action_delay:
            time.sleep(self._action_delay)

    def _shot_dir(self) -> Path:
        if self._screenshot_dir is None:
            import tempfile

            self._screenshot_dir = Path(tempfile.gettempdir()) / _SCREENSHOT_PREFIX
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        return self._screenshot_dir

    def _prepare(
        self, data: bytes, width: int, height: int, region: Region
    ) -> tuple[Frame, bytes]:
        """Fit the capture to model limits, returning the frame and PNG bytes."""
        fit_w, fit_h = fit_within(
            width,
            height,
            max_long_edge=self._max_long_edge,
            max_pixels=self._max_pixels,
        )
        if (fit_w, fit_h) != (width, height):
            try:
                from PIL import Image
            except ImportError as exc:  # pragma: no cover - depends on size
                raise ComputerUseError(
                    "Downscaling this screenshot needs Pillow. Install with: "
                    "uv sync --extra computeruse"
                ) from exc
            image = Image.open(BytesIO(data)).convert("RGB").resize((fit_w, fit_h))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            data = buffer.getvalue()
        return Frame(region=region, image_width=fit_w, image_height=fit_h), data

    # ── Tools ─────────────────────────────────────────────────────────────

    def get_tools(self) -> list[AgentTool]:
        @tool
        def computer_screenshot(
            x: int | None = None,
            y: int | None = None,
            width: int | None = None,
            height: int | None = None,
        ) -> str | ImageToolResult:
            """Capture the screen (or a region) and return it as an image.

            Call this before acting: the returned caption states the coordinate
            space to use in the other computer_* tools. Optional x/y/width/
            height capture just that region.
            """
            return self._screenshot(x, y, width, height)

        @tool
        def computer_move(x: int, y: int) -> str:
            """Move the mouse pointer to (x, y) in the last screenshot's space."""
            return self._move(x, y)

        @tool
        def computer_click(
            x: int, y: int, button: str = "left", clicks: int = 1
        ) -> str:
            """Click at (x, y). button: left/middle/right; clicks for double."""
            return self._click(x, y, button, clicks)

        @tool
        def computer_drag(
            x1: int, y1: int, x2: int, y2: int, duration: float = 0.3
        ) -> str:
            """Drag from (x1, y1) to (x2, y2) holding the left button."""
            return self._drag(x1, y1, x2, y2, duration)

        @tool
        def computer_scroll(x: int, y: int, dx: int = 0, dy: int = 3) -> str:
            """Scroll at (x, y). Positive dy scrolls down, dx right; units are
            wheel notches."""
            return self._scroll(x, y, dx, dy)

        @tool
        def computer_type(text: str) -> str:
            """Type text as keyboard input into the focused control."""
            return self._type(text)

        @tool
        def computer_key(keys: list[str]) -> str:
            """Press keys together, e.g. ["ctrl", "shift", "s"] or ["enter"]."""
            return self._key(keys)

        @tool
        def computer_wait(seconds: float = 1.0) -> str:
            """Wait for the UI to settle before the next screenshot."""
            return self._wait(seconds)

        # Input tools deliberately carry NO hints by default, so the gate
        # leaves them at its default level (allow) and the agent never prompts
        # for computer use. Setting ``require_confirmation`` restores the
        # destructive/open-world hints, which resolve to `ask` and fail closed
        # in one-shot mode.
        if self._require_confirmation:
            for input_tool in (
                computer_move,
                computer_click,
                computer_drag,
                computer_scroll,
                computer_type,
                computer_key,
            ):
                input_tool.metadata = dict(_MUTATING_HINT)
        for read_tool in (computer_screenshot, computer_wait):
            read_tool.metadata = dict(_READ_ONLY_HINT)

        return [
            computer_screenshot,
            computer_move,
            computer_click,
            computer_drag,
            computer_scroll,
            computer_type,
            computer_key,
            computer_wait,
        ]

    # ── Tool implementations ──────────────────────────────────────────────

    def _screenshot(
        self,
        x: int | None,
        y: int | None,
        width: int | None,
        height: int | None,
    ) -> str | ImageToolResult:
        if x is not None and y is not None and width is not None and height is not None:
            region: Region | None = Region(
                x=int(x), y=int(y), width=int(width), height=int(height)
            )
        elif x is None and y is None and width is None and height is None:
            region = None
        else:
            return (
                "To capture a region pass all of x, y, width and height; "
                "to capture the whole screen pass none of them."
            )
        backend = self._backend_impl()
        try:
            raster = backend.capture(region)
        except ComputerUseError as exc:
            return f"Screenshot failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface backend errors to the model
            return f"Screenshot failed: {type(exc).__name__}: {exc}"

        if region is None:
            region = Region(0, 0, raster.width, raster.height)
        frame, data = self._prepare(raster.data, raster.width, raster.height, region)
        self._frame = frame

        self._shot_index += 1
        path = self._shot_dir() / f"frame-{self._shot_index:05d}.png"
        path.write_bytes(data)
        return ImageToolResult(
            text=frame.describe(),
            image=ImageBlock(source=f"file://{path}", media_type="image/png"),
        )

    def _move(self, x: int, y: int) -> str:
        nx, ny = self._to_native(x, y)
        try:
            self._backend_impl().move(nx, ny)
        except Exception as exc:  # noqa: BLE001
            return f"move failed: {type(exc).__name__}: {exc}"
        self._settle()
        return f"Moved to ({nx}, {ny})."

    def _click(self, x: int, y: int, button: str, clicks: int) -> str:
        if clicks < 1:
            return "clicks must be >= 1"
        nx, ny = self._to_native(x, y)
        try:
            self._backend_impl().click(nx, ny, button=button, clicks=clicks)
        except Exception as exc:  # noqa: BLE001
            return f"click failed: {type(exc).__name__}: {exc}"
        self._settle()
        label = "click" if clicks == 1 else f"{clicks}x click"
        return f"{label} ({button}) at ({nx}, {ny})."

    def _drag(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> str:
        nx1, ny1 = self._to_native(x1, y1)
        nx2, ny2 = self._to_native(x2, y2)
        try:
            self._backend_impl().drag(
                nx1, ny1, nx2, ny2, duration=max(0.0, float(duration))
            )
        except Exception as exc:  # noqa: BLE001
            return f"drag failed: {type(exc).__name__}: {exc}"
        self._settle()
        return f"Dragged ({nx1}, {ny1}) -> ({nx2}, {ny2})."

    def _scroll(self, x: int, y: int, dx: int, dy: int) -> str:
        nx, ny = self._to_native(x, y)
        try:
            self._backend_impl().scroll(nx, ny, dx=int(dx), dy=int(dy))
        except Exception as exc:  # noqa: BLE001
            return f"scroll failed: {type(exc).__name__}: {exc}"
        self._settle()
        return f"Scrolled by (dx={int(dx)}, dy={int(dy)}) at ({nx}, {ny})."

    def _type(self, text: str) -> str:
        if not text:
            return "text must not be empty"
        try:
            self._backend_impl().type_text(text)
        except Exception as exc:  # noqa: BLE001
            return f"type failed: {type(exc).__name__}: {exc}"
        self._settle()
        preview = text if len(text) <= 60 else text[:57] + "..."
        return f"Typed {preview!r}."

    def _key(self, keys: list[str]) -> str:
        if not keys:
            return "keys must not be empty"
        try:
            self._backend_impl().press(list(keys))
        except Exception as exc:  # noqa: BLE001
            return f"key failed: {type(exc).__name__}: {exc}"
        self._settle()
        return f"Pressed {'+'.join(keys)}."

    def _wait(self, seconds: float) -> str:
        delay = min(max(float(seconds), 0.0), 60.0)
        time.sleep(delay)
        return f"Waited {delay:.1f}s."


def create_plugin() -> ComputerUsePlugin:
    """Factory for the path-based loader.

    Style: ``path:./phoson_plugin_computeruse/_plugin.py``.
    """
    return ComputerUsePlugin()


__all__ = ["ComputerUsePlugin", "create_plugin"]
