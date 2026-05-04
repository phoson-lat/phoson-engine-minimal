"""
Module for plugin system infrastructure.
"""

from abc import ABC, abstractmethod
from typing import Any
from collections.abc import Callable

from phoson_agent.models import AgentTool
from phoson_agent.middleware import AgentMiddleware


class Plugin(ABC):
    """
    Base class for all phoson-agent plugins.
    
    Plugins can provide:
    - Tools (functions that the agent can call)
    - Middlewares (hooks into the agent lifecycle)
    - Configuration (settings for the plugin)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for the plugin."""
        ...

    @property
    def version(self) -> str:
        """Plugin version (semver recommended)."""
        return "0.1.0"

    @property
    def description(self) -> str:
        """Human-readable description of what the plugin does."""
        return ""

    def get_tools(self) -> list[AgentTool]:
        """
        Returns a list of tools provided by this plugin.
        Called during plugin initialization.
        """
        return []

    def get_middlewares(self) -> list[AgentMiddleware]:
        """
        Returns a list of middlewares provided by this plugin.
        Called during plugin initialization.
        """
        return []

    def configure(self, config: dict[str, Any]) -> None:
        """
        Configure the plugin with user-provided settings.
        Called before get_tools() and get_middlewares().
        
        Args:
            config: Configuration dictionary for this plugin
        """
        pass

    def initialize(self) -> None:
        """
        Initialize the plugin (setup connections, load resources, etc).
        Called after configure() but before the agent starts running.
        """
        pass

    def cleanup(self) -> None:
        """
        Cleanup plugin resources (close connections, save state, etc).
        Called when the agent is shutting down.
        """
        pass


class PluginSpec:
    """
    Specification for loading a plugin.
    Can be a string (plugin name/path) or a dict with config.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        instance: Plugin | None = None,
    ):
        self.name = name
        self.config = config or {}
        self.instance = instance

    @classmethod
    def from_value(cls, value: str | dict[str, Any] | Plugin) -> "PluginSpec":
        """
        Create a PluginSpec from various input formats:
        - str: plugin name/path
        - dict: {"name": "plugin-name", "config": {...}}
        - Plugin: already instantiated plugin
        """
        if isinstance(value, Plugin):
            return cls(name=value.name, instance=value)

        if isinstance(value, str):
            return cls(name=value)

        if isinstance(value, dict):
            name = value.get("name")
            if not name:
                raise ValueError("Plugin dict must have 'name' key")
            config = value.get("config", {})
            return cls(name=name, config=config)

        raise TypeError(
            f"Plugin must be str, dict, or Plugin instance, got {type(value)}"
        )


PluginLoader = Callable[[str], Plugin]
