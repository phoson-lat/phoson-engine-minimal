"""Textual TUI for the Phoson CLI (optional ``tui`` extra).

Importing this package requires ``textual`` — ``__main__`` only
imports it when the user passes ``--textual``.
"""

from .app import PhosonTextualApp

__all__ = ["PhosonTextualApp"]
