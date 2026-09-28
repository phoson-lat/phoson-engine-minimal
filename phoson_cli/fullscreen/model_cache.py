"""Background-refreshed model id cache (moved to :mod:`phoson_cli.model_cache`).

Re-exported here for import compatibility; the implementation is shared
with the classic REPL.
"""

from ..model_cache import ModelCache  # noqa: F401 - re-exported

__all__ = ["ModelCache"]
