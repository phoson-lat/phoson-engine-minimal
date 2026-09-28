"""Session list cache (moved to :mod:`phoson_cli.session_cache`).

Re-exported here for import compatibility; the implementation is shared
with the classic REPL.
"""

from ..session_cache import SessionListCache  # noqa: F401 - re-exported

__all__ = ["SessionListCache"]
