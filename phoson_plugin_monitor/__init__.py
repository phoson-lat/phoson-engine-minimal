"""
Phoson Monitor Plugin

Long-running background monitors (interval, file, command) that outlive
the current agent run and re-activate the agent when their condition
fires. State (registry + wake queue) is persisted under ``data_dir``
(default ``~/.phoson/monitors/``) so monitors and pending wakes survive
process restarts; the next host resurrects running monitors.
"""

from ._plugin import MonitorPlugin, create_plugin, render_wake_message
from .storage import WakeEvent, WakeQueue, MonitorDef, MonitorStore

__version__ = "0.1.0"

# Export plugin instance. NOTE: the module file is named `_plugin.py` (not
# `plugin.py`) so this `plugin = ...` attribute does not shadow the
# submodule attribute — otherwise `import phoson_plugin_monitor.plugin as m`
# would bind the instance instead of the module.
plugin = MonitorPlugin()

__all__ = [
    "MonitorPlugin",
    "create_plugin",
    "render_wake_message",
    "MonitorDef",
    "MonitorStore",
    "WakeEvent",
    "WakeQueue",
    "plugin",
]
