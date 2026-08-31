"""
Phoson CLI module.

This module provides the interactive command-line interface for the Phoson agent,
including session management, tool execution, and conversation history.

``PhosonRepl`` is re-exported at package level for backwards compatibility,
but it is imported *lazily* (PEP 562) so that importing lightweight,
UI-free submodules — most importantly :mod:`phoson_cli.config` — does NOT
drag in ``prompt_toolkit``/``rich``. That keeps the package importable by
non-interactive hosts (e.g. an embedded Phoson-Core) that only need the
config/factory helpers, without paying for the TUI stack.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .repl import PhosonRepl

__all__ = ["PhosonRepl"]


def __getattr__(name: str) -> Any:
    """Lazily resolve ``PhosonRepl`` on first attribute access (PEP 562).

    Importing ``phoson_cli`` (or ``phoson_cli.config`` etc.) must not load
    the heavy TUI stack; only ``from phoson_cli import PhosonRepl`` does.
    """
    if name == "PhosonRepl":
        from .repl import PhosonRepl

        return PhosonRepl
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
