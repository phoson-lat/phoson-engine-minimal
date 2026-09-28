"""Completers for the full-screen input line.

The implementations moved to :mod:`phoson_cli.arg_completers` so the
classic REPL can share them without importing the full-screen package
(``phoson_cli.fullscreen`` pulls in ``PhosonApp``). This module re-exports
them for import compatibility.
"""

from ..arg_completers import (
    PathCompleter,  # noqa: F401 - re-exported for import compatibility
    SlashCompleter,  # noqa: F401 - re-exported for import compatibility
    ModelArgCompleter,  # noqa: F401 - re-exported for import compatibility
    ResumeArgCompleter,  # noqa: F401 - re-exported for import compatibility
    StaticArgCompleter,  # noqa: F401 - re-exported for import compatibility
    SessionsArgCompleter,  # noqa: F401 - re-exported for import compatibility
)

__all__ = [
    "SlashCompleter",
    "PathCompleter",
    "ModelArgCompleter",
    "SessionsArgCompleter",
    "ResumeArgCompleter",
    "StaticArgCompleter",
]
