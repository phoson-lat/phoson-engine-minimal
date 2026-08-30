"""Installable community plugin example for the Phoson plugin platform."""

from .plugin import CommunityExamplePlugin, create_plugin

plugin = CommunityExamplePlugin()

__all__ = ["CommunityExamplePlugin", "create_plugin", "plugin"]
