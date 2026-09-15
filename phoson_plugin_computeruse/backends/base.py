"""Backend contract for Computer Use.

A backend is the thin, deterministic executor: capture pixels and inject input
in **native** coordinates. Policy (permissions, planning, coordinate mapping)
lives above it in :mod:`phoson_plugin_computeruse._plugin`.

Deliberately *not* PyAutoGUI/pynput: both are X11-centric and fail on native
Wayland. Concrete backends live in this package (``x11``, ``macos``, ``fake``).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..geometry import Region


class ComputerUseError(Exception):
    """Raised when a backend is unavailable, misconfigured, or fails."""


@dataclass(frozen=True)
class RasterImage:
    """An encoded image plus its pixel dimensions."""

    data: bytes
    width: int
    height: int
    #: ``image/png`` today; kept explicit so encoders can evolve.
    media_type: str = "image/png"


class ComputerBackend(ABC):
    """Capture the screen and inject mouse/keyboard input in native pixels.

    All coordinates are absolute in the virtual screen's coordinate space
    (origin at the top-left, ``screen_size()`` bounding box).
    """

    #: Short identifier surfaced in errors and telemetry (``x11``, ``macos``).
    name: str = "backend"

    @abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """Return the virtual screen size as ``(width, height)`` in native px."""

    @abstractmethod
    def capture(self, region: Region | None = None) -> RasterImage:
        """Capture the whole screen or ``region`` (native coords) as an image."""

    @abstractmethod
    def move(self, x: int, y: int) -> None:
        """Move the pointer to ``(x, y)``."""

    @abstractmethod
    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        clicks: int = 1,
    ) -> None:
        """Move to ``(x, y)`` and click ``clicks`` times with ``button``."""

    @abstractmethod
    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration: float = 0.3,
    ) -> None:
        """Press at ``(x1, y1)``, move to ``(x2, y2)``, release."""

    @abstractmethod
    def scroll(self, x: int, y: int, *, dx: int, dy: int) -> None:
        """Scroll at ``(x, y)``. Positive ``dy`` scrolls down, ``dx`` right."""

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type ``text`` as keyboard input."""

    @abstractmethod
    def press(self, keys: list[str]) -> None:
        """Press named keys together, e.g. ``["ctrl", "shift", "s"]``."""

    def close(self) -> None:
        """Release backend resources. Safe to call more than once."""
