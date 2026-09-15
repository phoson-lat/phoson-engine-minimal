"""Deterministic, dependency-free backend for tests and dry runs.

Records every action so tests can assert on the exact input sequence, and
returns a valid solid-colour PNG generated with :mod:`zlib` (so tests do not
need Pillow). ``screen_size`` is configurable to exercise coordinate mapping.
"""

import zlib
import struct
from dataclasses import field, dataclass

from .base import RasterImage, ComputerBackend
from ..geometry import Region


def _png(width: int, height: int, rgb: tuple[int, int, int] = (24, 24, 32)) -> bytes:
    """Encode a solid RGB image as PNG (no third-party dependency)."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


@dataclass
class FakeBackend(ComputerBackend):
    """In-memory backend that records actions instead of performing them."""

    name: str = "fake"
    width: int = 1920
    height: int = 1080
    color: tuple[int, int, int] = (24, 24, 32)
    #: Every action, in order, as ``(action, args...)`` tuples.
    actions: list[tuple] = field(default_factory=list)
    captures: list[Region | None] = field(default_factory=list)
    closed: bool = False

    def screen_size(self) -> tuple[int, int]:
        return self.width, self.height

    def capture(self, region: Region | None = None) -> RasterImage:
        if self.closed:
            raise RuntimeError("backend is closed")
        self.captures.append(region)
        if region is None:
            w, h = self.width, self.height
        else:
            w, h = region.width, region.height
        return RasterImage(data=_png(w, h, self.color), width=w, height=h)

    def move(self, x: int, y: int) -> None:
        self.actions.append(("move", int(x), int(y)))

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self.actions.append(("click", int(x), int(y), button, int(clicks)))

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3
    ) -> None:
        self.actions.append(
            ("drag", int(x1), int(y1), int(x2), int(y2), float(duration))
        )

    def scroll(self, x: int, y: int, *, dx: int, dy: int) -> None:
        self.actions.append(("scroll", int(x), int(y), int(dx), int(dy)))

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))

    def press(self, keys: list[str]) -> None:
        self.actions.append(("press", tuple(keys)))

    def close(self) -> None:
        self.closed = True
