"""Per-session tool render registry (UI-free).

Plugins may contribute :class:`~phoson_agent.ToolRenderSpec` entries (verb
+ icon) for the tools they own. The session runtime validates them once per
engine build; the Rich cards in :mod:`phoson_cli.formatting` read the
result. Kept apart from ``formatting`` so the runtime can build the
registry without importing the Rich renderers.
"""

from typing import Final
from dataclasses import dataclass
from collections.abc import Sequence, Collection

from phoson_agent import Plugin, ToolRenderSpec

#: Human verb per tool — the card headline ("writing file src/x.py").
#: Subagent tools keep their dedicated start/done lines, so they are absent.
_TOOL_VERBS: Final[dict[str, str]] = {
    "read_file": "reading file",
    "write_file": "writing file",
    "patch_file": "editing file",
    "list_dir": "listing directory",
    "grep": "searching files",
    "glob": "globbing files",
    "view_image": "viewing image",
    "bash": "running command",
    "skill": "loading skill",
    "web_search": "searching the web",
    "web_fetch": "fetching page",
}


@dataclass(frozen=True)
class ToolRenderRegistry:
    """Immutable per-session mapping of tool names to presentation specs."""

    specs: dict[str, ToolRenderSpec]

    def get(self, tool_name: str) -> ToolRenderSpec | None:
        return self.specs.get(tool_name)


def build_tool_render_registry(
    plugins: Sequence[Plugin], tool_names: Collection[str]
) -> ToolRenderRegistry:
    """Validate visual specs contributed by the currently loaded plugins.

    Built-in tools keep the visual treatment defined in this module. A plugin
    may only style a tool it owns, which prevents dependencies from silently
    changing the CLI's rendering of core tools.
    """
    native_names = set(_TOOL_VERBS) | {"agent", "agents"}
    available = set(tool_names)
    specs: dict[str, ToolRenderSpec] = {}
    for plugin in plugins:
        try:
            plugin_specs = plugin.get_tool_render_specs()
        except Exception as exc:
            raise ValueError(
                f"Plugin {plugin.name!r} failed while listing tool render specs: {exc}"
            ) from exc
        for spec in plugin_specs:
            if not isinstance(spec, ToolRenderSpec):
                raise ValueError(
                    f"Plugin {plugin.name!r} returned {type(spec).__name__} "
                    "instead of ToolRenderSpec"
                )
            if not spec.tool_name or spec.tool_name not in available:
                raise ValueError(
                    f"Plugin {plugin.name!r} render spec references unknown tool "
                    f"{spec.tool_name!r}"
                )
            if spec.tool_name in native_names:
                raise ValueError(
                    f"Plugin {plugin.name!r} cannot override built-in tool "
                    f"{spec.tool_name!r}"
                )
            if not spec.verb.strip() or not spec.icon.strip():
                raise ValueError(
                    f"Plugin {plugin.name!r} render spec for {spec.tool_name!r} "
                    "requires non-empty verb and icon"
                )
            if spec.tool_name in specs:
                raise ValueError(
                    f"Multiple plugins define a render spec for {spec.tool_name!r}"
                )
            specs[spec.tool_name] = spec
    return ToolRenderRegistry(specs)


__all__ = ["ToolRenderRegistry", "build_tool_render_registry"]
