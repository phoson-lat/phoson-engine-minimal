"""Geometry for Computer Use: capture regions and coordinate mapping.

The single most common failure in computer-use agents is a mismatch between
the coordinate space the model sees (a *downscaled screenshot*) and the space
the input backend acts in (*native* screen pixels) — see the SOTA research.
Everything here exists to make that mapping explicit and testable.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    """A rectangle in **native input coordinates** (top-left origin)."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Region width/height must be positive")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def clamp(self, x: int, y: int) -> tuple[int, int]:
        """Clamp a native point into this region (inclusive of the last px)."""
        cx = min(max(int(x), self.x), self.right - 1)
        cy = min(max(int(y), self.y), self.bottom - 1)
        return cx, cy


@dataclass(frozen=True)
class Frame:
    """One capture: the native region plus the size the model actually sees.

    ``image_width``/``image_height`` are the dimensions of the image sent to
    the model (after downscaling). Coordinates returned by the model are in
    that space and are mapped back to native input coordinates by
    :meth:`to_native`.
    """

    region: Region
    image_width: int
    image_height: int

    def __post_init__(self) -> None:
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("Frame image dimensions must be positive")

    @property
    def scale(self) -> tuple[float, float]:
        """Native pixels per displayed pixel, as ``(sx, sy)``."""
        return (
            self.region.width / self.image_width,
            self.region.height / self.image_height,
        )

    def to_native(self, x: int, y: int) -> tuple[int, int]:
        """Map a displayed-image coordinate to a clamped native coordinate."""
        sx, sy = self.scale
        nx = self.region.x + int(round(x * sx))
        ny = self.region.y + int(round(y * sy))
        return self.region.clamp(nx, ny)

    def describe(self) -> str:
        """A caption telling the model exactly which coordinate space to use."""
        sx, sy = self.scale
        note = ""
        if (sx, sy) != (1.0, 1.0):
            note = (
                f" (downscaled from {self.region.width}x{self.region.height} "
                f"native px; coordinates are scaled automatically)"
            )
        return (
            f"Screen capture {self.image_width}x{self.image_height} px. "
            f"Pass coordinates as x in [0, {self.image_width - 1}], "
            f"y in [0, {self.image_height - 1}]{note}."
        )


def fit_within(
    width: int,
    height: int,
    *,
    max_long_edge: int = 1568,
    max_pixels: int = 1_150_000,
) -> tuple[int, int]:
    """Largest size fitting the constraints, never upscaling.

    Mirrors the image limits most vision APIs enforce: if a screenshot exceeds
    them the provider silently downscales it, and the model then clicks on a
    degraded image — the documented cause of mis-clicks. Downscaling here keeps
    the model's perceived image and our coordinate space identical.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width/height must be positive")
    if max_long_edge <= 0 or max_pixels <= 0:
        raise ValueError("constraints must be positive")

    long_edge = max(width, height)
    scale = min(1.0, max_long_edge / long_edge)

    total = width * height * scale * scale
    if total > max_pixels:
        scale *= math.sqrt(max_pixels / total)

    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    return new_w, new_h
