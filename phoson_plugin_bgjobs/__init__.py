"""
Phoson Background Jobs Plugin

Run shell commands as background jobs (one-shot, not polling) that outlive
the current agent run and **re-activate the agent** when they finish, carrying
the exit code, an output tail and the duration. State (registry + wake queue)
is persisted under ``data_dir`` (default ``~/.phoson/bgjobs/``) so jobs and
pending wakes survive process restarts; the next host reconciles them.
"""

from ._plugin import BgJobsPlugin, create_plugin, render_wake_message
from .storage import JobDef, JobStore, WakeEvent, WakeQueue

__version__ = "0.1.0"

# Export plugin instance. NOTE: the module file is named `_plugin.py` (not
# `plugin.py`) so this `plugin = ...` attribute does not shadow the
# submodule attribute.
plugin = BgJobsPlugin()

__all__ = [
    "BgJobsPlugin",
    "create_plugin",
    "render_wake_message",
    "JobDef",
    "JobStore",
    "WakeEvent",
    "WakeQueue",
    "plugin",
]
