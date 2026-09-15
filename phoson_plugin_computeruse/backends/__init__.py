"""Computer Use backends.

Importing this package must never require an optional dependency: the heavy
imports (``mss``, ``Xlib``, ``Quartz``) live inside the backend modules and are
performed lazily by :func:`factory.detect_backend`.
"""

from .base import RasterImage, ComputerBackend, ComputerUseError
from .fake import FakeBackend

__all__ = [
    "ComputerBackend",
    "ComputerUseError",
    "RasterImage",
    "FakeBackend",
]
