"""
Module for loading and managing plugins.
"""

import importlib
import sys
from pathlib import Path
from typing import Any

from phoson_agent.plugin import Plugin, PluginSpec, PluginLoader


class PluginRegistry:
    """
    Registry for plugin loaders.
    Supports loading plugins from:
    - Python packages (installed via pip)
    - Entry points (setuptools entry points)
    - Local paths
    - Custom loaders
    """

    def __init__(self) -> None:
        self._loaders: dict[str, PluginLoader] = {}
        self._register_default_loaders()

    def _register_default_loaders(self) -> None:
        """Register built-in plugin loaders."""
        self.register_loader("package", self._load_from_package)
        self.register_loader("path", self._load_from_path)
        self.register_loader("entrypoint", self._load_from_entrypoint)

    def register_loader(self, prefix: str, loader: PluginLoader) -> None:
        """
        Register a custom plugin loader.
        
        Args:
            prefix: URL-like prefix (e.g., "github", "http")
            loader: Function that takes a plugin name and returns a Plugin instance
        """
        self._loaders[prefix] = loader

    def load(self, spec: PluginSpec) -> Plugin:
        """
        Load a plugin from a specification.
        
        Supports formats:
        - "phoson-plugin-mcp" -> package loader
        - "package:phoson-plugin-mcp" -> explicit package loader
        - "path:/path/to/plugin.py" -> local file
        - "entrypoint:my-plugin" -> setuptools entry point
        
        Args:
            spec: Plugin specification
            
        Returns:
            Loaded and configured Plugin instance
        """
        if spec.instance:
            plugin = spec.instance
        else:
            name = spec.name
            loader_name = "package"  # default

            # Parse loader prefix
            if ":" in name:
                loader_name, name = name.split(":", 1)

            loader = self._loaders.get(loader_name)
            if not loader:
                raise ValueError(
                    f"Unknown plugin loader '{loader_name}'. "
                    f"Available: {list(self._loaders.keys())}"
                )

            plugin = loader(name)

        # Always configure and initialize, even for instances
        plugin.configure(spec.config)
        plugin.initialize()

        return plugin

    def _load_from_package(self, name: str) -> Plugin:
        """
        Load plugin from an installed Python package.
        
        Convention: package must have a `plugin` attribute at top level
        that is a Plugin instance or a callable that returns one.
        
        Example:
            # phoson_plugin_mcp/__init__.py
            from .plugin import MCPPlugin
            plugin = MCPPlugin()
        """
        try:
            module = importlib.import_module(name.replace("-", "_"))
        except ImportError as exc:
            raise ImportError(
                f"Failed to import plugin package '{name}'. "
                f"Is it installed? (pip install {name})"
            ) from exc

        if not hasattr(module, "plugin"):
            raise AttributeError(
                f"Plugin package '{name}' must have a 'plugin' attribute"
            )

        plugin_attr = getattr(module, "plugin")

        if isinstance(plugin_attr, Plugin):
            return plugin_attr

        if callable(plugin_attr):
            result = plugin_attr()
            if not isinstance(result, Plugin):
                raise TypeError(
                    f"Plugin factory in '{name}' must return a Plugin instance"
                )
            return result

        raise TypeError(
            f"Plugin attribute in '{name}' must be a Plugin instance or factory"
        )

    def _load_from_path(self, path_str: str) -> Plugin:
        """
        Load plugin from a local Python file.
        
        The file must have a `plugin` variable or `create_plugin()` function.
        """
        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            raise FileNotFoundError(f"Plugin file not found: {path}")

        if not path.is_file():
            raise ValueError(f"Plugin path must be a file: {path}")

        # Add parent directory to sys.path temporarily
        parent_dir = str(path.parent)
        sys.path.insert(0, parent_dir)

        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if not spec or not spec.loader:
                raise ImportError(f"Failed to load plugin from {path}")

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Try to get plugin
            if hasattr(module, "plugin"):
                plugin_attr = getattr(module, "plugin")
                if isinstance(plugin_attr, Plugin):
                    return plugin_attr
                if callable(plugin_attr):
                    return plugin_attr()

            if hasattr(module, "create_plugin"):
                factory = getattr(module, "create_plugin")
                return factory()

            raise AttributeError(
                f"Plugin file '{path}' must have 'plugin' or 'create_plugin()'"
            )
        finally:
            sys.path.remove(parent_dir)

    def _load_from_entrypoint(self, name: str) -> Plugin:
        """
        Load plugin from a setuptools entry point.
        
        Entry points should be registered in pyproject.toml:
        [project.entry-points."phoson.plugins"]
        my-plugin = "my_package.plugin:create_plugin"
        """
        try:
            from importlib.metadata import entry_points
        except ImportError:
            from importlib_metadata import entry_points  # type: ignore

        group = "phoson.plugins"
        eps = entry_points()

        # Handle both old and new entry_points() API
        if hasattr(eps, "select"):
            matches = eps.select(group=group, name=name)
        else:
            matches = eps.get(group, [])
            matches = [ep for ep in matches if ep.name == name]

        if not matches:
            raise ValueError(
                f"No entry point found for plugin '{name}' in group '{group}'"
            )

        ep = list(matches)[0]
        factory = ep.load()

        if isinstance(factory, Plugin):
            return factory

        if callable(factory):
            result = factory()
            if not isinstance(result, Plugin):
                raise TypeError(f"Entry point '{name}' must return a Plugin instance")
            return result

        raise TypeError(f"Entry point '{name}' must be a Plugin or factory")


# Global plugin registry
_default_registry = PluginRegistry()


def load_plugin(spec: str | dict[str, Any] | Plugin) -> Plugin:
    """
    Convenience function to load a plugin using the default registry.
    
    Args:
        spec: Plugin specification (string, dict, or Plugin instance)
        
    Returns:
        Loaded Plugin instance
    """
    plugin_spec = PluginSpec.from_value(spec)
    return _default_registry.load(plugin_spec)


def register_loader(prefix: str, loader: PluginLoader) -> None:
    """
    Register a custom plugin loader in the default registry.
    
    Args:
        prefix: URL-like prefix (e.g., "github", "http")
        loader: Function that takes a plugin name and returns a Plugin instance
    """
    _default_registry.register_loader(prefix, loader)
