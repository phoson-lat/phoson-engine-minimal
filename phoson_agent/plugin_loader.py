"""
Module for loading and managing plugins.
"""

import sys
import importlib
import importlib.util
from typing import Any
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator

from phoson_agent.plugin import Plugin, PluginSpec, PluginLoader
from phoson_agent.exceptions import PhosonPluginLoadError, PhosonPluginConfigError


@contextmanager
def _sys_path_guard(directory: str) -> Iterator[None]:
    """Temporarily prepend ``directory`` to ``sys.path``.

    Restores the *exact* previous state on exit (snapshot-restore instead of
    ``remove``), so it is safe even if the directory was already present in
    ``sys.path`` or the list is mutated while the guard is active.
    """
    original = list(sys.path)
    sys.path.insert(0, directory)
    try:
        yield
    finally:
        sys.path[:] = original


def _resolve_plugin(attr: Any, source: str) -> Plugin:
    """Resolve a module attribute to a :class:`Plugin` instance.

    Accepts either a :class:`Plugin` instance directly or a zero-argument
    callable (factory) that returns one.

    Args:
        attr: The object to resolve — either a Plugin or a factory.
        source: Human-readable description of the origin (package/path/entry
                point name) used in error messages.

    Returns:
        A :class:`Plugin` instance.

    Raises:
        PhosonPluginLoadError: If ``attr`` is neither a Plugin nor a callable,
            or if the callable returns a non-Plugin value.
    """
    if isinstance(attr, Plugin):
        return attr
    if callable(attr):
        result = attr()
        if not isinstance(result, Plugin):
            raise PhosonPluginLoadError(
                f"Plugin factory from '{source}' must return a Plugin instance, "
                f"got {type(result).__name__}"
            )
        return result
    raise PhosonPluginLoadError(
        f"Plugin from '{source}' must be a Plugin instance or factory, "
        f"got {type(attr).__name__}"
    )


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

        Raises:
            PhosonPluginConfigError: If the loader prefix is unknown.
            PhosonPluginLoadError: If the plugin cannot be loaded.
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
                raise PhosonPluginConfigError(
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
            from ._plugin import MCPPlugin
            plugin = MCPPlugin()

        Raises:
            PhosonPluginLoadError: If the package cannot be imported or has
                no valid ``plugin`` attribute.
        """
        try:
            module = importlib.import_module(name.replace("-", "_"))
        except ImportError as exc:
            raise PhosonPluginLoadError(
                f"Failed to import plugin package '{name}'. "
                f"Is it installed? (pip install {name})"
            ) from exc

        if not hasattr(module, "plugin"):
            raise PhosonPluginLoadError(
                f"Plugin package '{name}' must have a 'plugin' attribute"
            )

        plugin_attr = getattr(module, "plugin")
        return _resolve_plugin(plugin_attr, name)

    def _load_from_path(self, path_str: str) -> Plugin:
        """
        Load plugin from a local Python file.

        The file must have a `plugin` variable or `create_plugin()` function.

        Raises:
            PhosonPluginLoadError: If the path is invalid or the plugin
                cannot be loaded.
        """
        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            raise PhosonPluginLoadError(f"Plugin file not found: {path}")

        if not path.is_file():
            raise PhosonPluginLoadError(f"Plugin path must be a file: {path}")

        # Prepend the parent directory so sibling imports inside the plugin
        # file resolve; the guard restores sys.path exactly on exit.
        with _sys_path_guard(str(path.parent)):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if not spec or not spec.loader:
                raise PhosonPluginLoadError(f"Failed to load plugin from {path}")

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Try to get plugin
            if hasattr(module, "plugin"):
                return _resolve_plugin(getattr(module, "plugin"), str(path))

            if hasattr(module, "create_plugin"):
                return _resolve_plugin(getattr(module, "create_plugin"), str(path))

            raise PhosonPluginLoadError(
                f"Plugin file '{path}' must have 'plugin' or 'create_plugin()'"
            )

    def _load_from_entrypoint(self, name: str) -> Plugin:
        """
        Load plugin from a setuptools entry point.

        Entry points should be registered in pyproject.toml:
        [project.entry-points."phoson.plugins"]
        my-plugin = "my_package.plugin:create_plugin"

        Raises:
            PhosonPluginLoadError: If the entry point is missing or invalid.
        """
        try:
            from importlib.metadata import entry_points
        except ImportError:
            from importlib_metadata import entry_points  # type: ignore

        group = "phoson.plugins"
        eps = entry_points()

        # Handle both old and new entry_points() API
        # Modern (3.10+) ``entry_points`` returns an ``EntryPoints`` object
        # with a ``select`` method. We only target Python 3.12+ in this
        # project, but we keep a fallback for the legacy dict-based shape
        # in case ``importlib_metadata`` is in use.
        if hasattr(eps, "select"):
            matches = list(eps.select(group=group, name=name))
        else:
            legacy = eps.get(group, []) if hasattr(eps, "get") else []  # type: ignore[union-attr]
            matches = [ep for ep in legacy if ep.name == name]

        if not matches:
            raise PhosonPluginLoadError(
                f"No entry point found for plugin '{name}' in group '{group}'"
            )

        ep = matches[0]
        return _resolve_plugin(ep.load(), name)


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
