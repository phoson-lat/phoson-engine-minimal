"""
Module for plugin system infrastructure.
"""

from abc import ABC, abstractmethod
from typing import Any
from collections.abc import Callable

from phoson_agent.models import AgentTool
from phoson_agent.exceptions import PhosonPluginConfigError
from phoson_agent.middleware import AgentMiddleware
from phoson_agent.cli_extensions import (
    CliCommandSpec,
    ThemeExtension,
    ToolRenderSpec,
)


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

    def get_commands(self) -> list[CliCommandSpec]:
        """Return slash commands contributed to a compatible CLI host.

        The default is empty so engine-only plugins stay fully compatible.
        Commands are declarative metadata; a host resolves ``handler`` on
        this loaded plugin instance and owns validation/dispatch.
        """
        return []

    def get_tool_render_specs(self) -> list[ToolRenderSpec]:
        """Return presentation metadata for tools owned by this plugin.

        A host decides how to render the neutral specs.  Plugins must not
        import a host UI toolkit merely to implement this optional hook.
        """
        return []

    def get_theme_extension(self) -> ThemeExtension | None:
        """Return one optional additional CLI theme, if the host supports it."""
        return None

    async def aclose(self) -> None:
        """Asynchronously close plugin resources.

        The default preserves the synchronous lifecycle contract by
        delegating to :meth:`cleanup`.  Plugins that own async pools or
        tasks may override this method; hosts should prefer it at shutdown.
        """
        self.cleanup()


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

        Raises:
            PhosonPluginConfigError: If the dict has no 'name' key.
            TypeError: If value is none of str, dict, or Plugin.
        """
        match value:
            case Plugin():
                return cls(name=value.name, instance=value)
            case str():
                return cls(name=value)
            case {"name": str(name), **rest}:
                config = rest.get("config", {})
                if not isinstance(config, dict):
                    raise PhosonPluginConfigError(
                        "Plugin config must be a dict if provided"
                    )
                return cls(name=name, config=config)
            case dict():
                raise PhosonPluginConfigError("Plugin dict must have 'name' key")
            case _:
                raise TypeError(
                    f"Plugin must be str, dict, or Plugin instance, got {type(value)}"
                )


PluginLoader = Callable[[str], Plugin]
