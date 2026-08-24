"""Shared session-runtime helpers (UI-independent).

Moved out of ``repl.py`` so the
:class:`~phoson_cli.controller.SessionController` — and any future
front end — can use them without importing the prompt_toolkit REPL.
``repl.py`` re-exports them for backward compatibility.
"""

import sys
import logging
import warnings
from typing import Any
from pathlib import Path
from datetime import UTC, datetime

from phoson_agent import Plugin

from .config import PhosonConfig

_LOGGER = logging.getLogger("phoson_cli.session_utils")

_SYSTEM_PROMPT_TEMPLATE = (
    "You are Phos, a terminal coding agent, created by the Phoson.lat team. "
    "You are running in working directory: {cwd}. "
    "You are working on a {so} system with a terminal. Current time is {time}. "
    "Available tools: {tools}.{mcp_note}"
    " Be concise, accurate, and use tools when needed."
)


def _local_time_info() -> tuple[str, str]:
    """Return ``(local_time, timezone_label)`` for the *system* timezone.

    Uses the process's local timezone (honouring the ``TZ`` environment
    variable) so the prompt is correct for users anywhere, not just a
    single hardcoded zone. Falls back to UTC if the local zone cannot be
    determined.
    """
    try:
        now = datetime.now().astimezone()
    except Exception:  # pragma: no cover - defensive; astimezone() rarely fails
        now = datetime.now(UTC)
    offset = now.strftime("%z")  # e.g. "+0200" / "-0500" / "+0000"
    tz_label = now.tzname() or "UTC"
    return (
        now.strftime("%Y-%m-%d %H:%M:%S"),
        f"{tz_label} (UTC{offset[:3]}:{offset[3:]})",
    )


def build_system_prompt(tools: list) -> str:
    """Build the system prompt for the loaded tools.

    The tool list is derived from the actual ``tools`` registry (so it can
    never drift from what the engine really exposes) and the clock uses the
    system's local timezone. Mentions the MCP tools currently loaded so the
    model knows they exist beyond the built-in set. Shared by the REPL and
    the one-shot mode.
    """
    has_mcp = any(t.name.startswith("mcp_") for t in tools)
    mcp_note = " MCP tools (names prefixed 'mcp_') are also available."
    if not has_mcp:
        mcp_note = ""
    tool_names = ", ".join(sorted(t.name for t in tools))
    local_time, tz_label = _local_time_info()
    return _SYSTEM_PROMPT_TEMPLATE.format(
        cwd=Path.cwd(),
        so=sys.platform,
        time=f"{local_time} Current timezone is: {tz_label}",
        tools=tool_names,
        mcp_note=mcp_note,
    )


def build_mcp_plugins(config: PhosonConfig) -> list[str | dict[str, Any] | Plugin]:
    """Resolve the MCP plugin specs for a configuration.

    Returns an empty list when MCP is disabled. Tries the in-tree
    ``phoson_plugin_mcp`` first; falls back to the path-based loader
    used during local development if the package is not installed.
    """
    if not config.enable_mcp:
        return []

    mcp_config = {
        "config_file": str(config.mcp_config_file),
        "tool_name_prefix": "mcp",
    }

    try:
        from phoson_plugin_mcp import MCPPlugin

        plugin = MCPPlugin()
        plugin.configure(mcp_config)
        return [plugin]
    except ImportError:
        return [
            {
                "name": "path:./phoson_plugin_mcp/_plugin.py",
                "config": mcp_config,
            }
        ]
    except Exception as exc:
        warnings.warn(
            f"Failed to initialise MCP plugin: {exc}", UserWarning, stacklevel=2
        )
        return []


async def close_plugins(plugins: list) -> None:
    """Close plugin instances, preferring async ``aclose()`` when present.

    Failures are logged, never raised — closing old resources must not
    take down whatever is rebuilding them.
    """
    for plugin in plugins:
        try:
            if hasattr(plugin, "aclose"):
                await plugin.aclose()
            else:
                plugin.cleanup()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Could not close plugin %r",
                getattr(plugin, "name", "?"),
                exc_info=True,
            )


__all__ = ["build_mcp_plugins", "build_system_prompt", "close_plugins"]
