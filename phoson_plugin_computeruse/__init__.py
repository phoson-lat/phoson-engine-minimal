"""Phoson Computer Use plugin (issue #223).

Gives the agent a *screenshot → decide → input* loop over the local desktop:
capture the screen as an image, then move, click, drag, scroll, type and press
keys through a platform backend (X11 / macOS; a fake backend backs tests).

Off by default — it controls the real desktop. The optional capture/input
dependencies ship as the ``[computeruse]`` extra.
"""

from ._plugin import ComputerUsePlugin, create_plugin
from .geometry import Frame, Region, fit_within
from .backends.base import RasterImage, ComputerBackend, ComputerUseError

__version__ = "0.1.0"

# NOTE: the module file is named `_plugin.py` (not `plugin.py`) so this
# `plugin = ...` attribute does not shadow the submodule attribute.
plugin = ComputerUsePlugin()

__all__ = [
    "ComputerUsePlugin",
    "create_plugin",
    "ComputerBackend",
    "ComputerUseError",
    "RasterImage",
    "Frame",
    "Region",
    "fit_within",
    "plugin",
]
