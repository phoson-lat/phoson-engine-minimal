"""
Phoson Peers Plugin

Named agents that talk to each other across CLI windows. Start each window
with ``phoson-cli --name <agent>``; agents on the same team (default
``default``) can then ``peer_ask`` / ``peer_send`` each other, and incoming
messages wake the recipient so you watch it answer in its own window.
State (presence + one-file-per-message mailboxes) lives under
``~/.phoson/peers/<team>/``.
"""

from ._plugin import (
    PEER_WAKE_HEADER,
    PeersPlugin,
    create_plugin,
    render_wake_message,
)
from .storage import PeerError, PeerStore, PeerMessage, PeerPresence

__version__ = "0.1.0"

# The plugin needs a name, so the package-level instance is unconfigured; the
# CLI builds a configured one (see phoson_cli.session_utils).
plugin = PeersPlugin()

__all__ = [
    "PEER_WAKE_HEADER",
    "PeerError",
    "PeerMessage",
    "PeerPresence",
    "PeerStore",
    "PeersPlugin",
    "create_plugin",
    "plugin",
    "render_wake_message",
]
