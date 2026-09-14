"""Phoson SSH Plugin

Run commands and move files on remote hosts over SSH (issue #169). Tools:
``ssh_exec`` (non-interactive command), ``ssh_copy_local_to_remote`` and
``ssh_copy_remote_to_local`` (SFTP), and ``ssh_hosts`` (list configured
aliases).

Hosts are named aliases resolved from plugin config over ``~/.ssh/config``.
Verification is strict (``~/.ssh/known_hosts``, never auto-add), auth is
key/agent only, and no PTY is allocated. Mutating tools default to ``ask``
through the permission gate's risk hints and fail closed in one-shot mode.
"""

from ._plugin import SSH_AVAILABLE, SshError, SshPlugin, create_plugin

__version__ = "0.1.0"

# Export plugin instance. NOTE: the module file is named `_plugin.py` (not
# `plugin.py`) so this `plugin = ...` attribute does not shadow the
# submodule attribute.
plugin = SshPlugin()

__all__ = [
    "SSH_AVAILABLE",
    "SshError",
    "SshPlugin",
    "create_plugin",
    "plugin",
]
