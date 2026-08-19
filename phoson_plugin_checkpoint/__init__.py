"""
Phoson Checkpoint Plugin

Postgres-backed SessionStorage for Phoson Agent. Own schema
(``phoson_checkpoint_*`` tables), no dependency on host-application tables.
"""

from ._plugin import CheckpointPlugin
from .storage import PostgresStorage

__version__ = "0.1.0"

# Export plugin instance (package-loader convention, see docs/plugins.md).
# The module file is `_plugin.py` (not `plugin.py`) so this attribute
# doesn't shadow the submodule attribute — see phoson_plugin_mcp.
plugin = CheckpointPlugin()

__all__ = ["CheckpointPlugin", "PostgresStorage", "plugin"]
