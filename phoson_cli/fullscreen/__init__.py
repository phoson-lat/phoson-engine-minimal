"""Full-screen prompt_toolkit front end for the Phoson CLI.

Replaces the classic line-by-line REPL (``phoson_cli.repl``) with a
persistent, scrollable chat pane, header/footer bars and floats for
pickers/confirmations, in the spirit of a single-window chat TUI built
directly on ``prompt_toolkit`` (no separate TUI framework).

:class:`PhosonApp` is the entry point; it wraps a
:class:`~phoson_cli.repl.PhosonRepl` (which owns the UI-independent
:class:`~phoson_cli.controller.SessionController`) and owns everything
presentation-specific: layout, key bindings, scrolling and rendering.
"""

from .app import PhosonApp

__all__ = ["PhosonApp"]
