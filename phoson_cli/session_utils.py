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
from .skills import discover_skills, render_skill_index
from .agents_md import load_agents_md

_LOGGER = logging.getLogger("phoson_cli.session_utils")

# The system prompt is the *stable prefix* of every request: it sits in
# front of the growing conversation history, so anything that changes
# between requests (a live clock, per-turn state) busts the provider's
# prompt cache for the whole prompt — see IMPROVEMENTS.md G2 / #69.
# Only date-level time (no hours/minutes) is safe here: it is constant
# for a full working day, which is all the model reliably needs to
# reason about "today"; for the exact wall clock it can run `date`
# (and `bash` is always in the tool registry in the CLI).
_SYSTEM_PROMPT_TEMPLATE = (
    "You are Phos, a terminal coding agent, created by the Phoson.lat team. "
    "You are running in working directory: {cwd}. "
    "You are working on a {so} system with a terminal. Current date is {time}. "
    "Available tools: {tools}.{mcp_note}"
    " Be concise, accurate, and use tools when needed."
    "{skills_block}{memory_block}"
)

#: Wrapper framing for the AGENTS.md memory injected into the prompt.
_MEMORY_BLOCK_TEMPLATE = (
    "\n\n# Project memory (AGENTS.md)"
    "\nInstructions from AGENTS.md/CLAUDE.md files in this repository and"
    " the user's home directory follow. They take precedence over your"
    " defaults when they conflict:\n\n{content}"
)

#: Default AGENTS.md budget (tokens) when the caller does not override it.
_AGENTS_MD_MAX_TOKENS_DEFAULT = 2000

#: Default skills-index budget (tokens) — IMPROVEMENTS.md G5.
_SKILLS_MAX_TOKENS_DEFAULT = 1000


def _local_time_info() -> tuple[str, str]:
    """Return ``(local_date, timezone_label)`` for the *system* timezone.

    Uses the process's local timezone (honouring the ``TZ`` environment
    variable) so the prompt is correct for users anywhere, not just a
    single hardcoded zone. Falls back to UTC if the local zone cannot be
    determined.

    Only the **date** is returned, deliberately not the full timestamp:
    the system prompt is the stable prefix of every request (prompt
    caching, IMPROVEMENTS.md G2), and a live clock would change the
    prefix on every turn, invalidating the cache for the entire prompt.
    The model can obtain the exact time with the ``bash`` tool when it
    genuinely needs it.
    """
    try:
        now = datetime.now().astimezone()
    except Exception:  # pragma: no cover - defensive; astimezone() rarely fails
        now = datetime.now(UTC)
    offset = now.strftime("%z")  # e.g. "+0200" / "-0500" / "+0000"
    tz_label = now.tzname() or "UTC"
    return (
        now.strftime("%Y-%m-%d"),
        f"{tz_label} (UTC{offset[:3]}:{offset[3:]})",
    )


def build_system_prompt(
    tools: list,
    agents_md_max_tokens: int | None = None,
    skills_max_tokens: int | None = None,
) -> str:
    """Build the system prompt for the loaded tools.

    The prompt is the **stable prefix** of every request (prompt caching
    — IMPROVEMENTS.md G2): it carries the date (not the live clock), the
    working directory, the platform and the tool list, all of which are
    constant for the lifetime of a session, so the provider's prompt
    cache can hold the entire prefix across turns.

    The tool list is derived from the actual ``tools`` registry (so it can
    never drift from what the engine really exposes) and the date uses the
    system's local timezone. Mentions the MCP tools currently loaded so the
    model knows they exist beyond the built-in set. AGENTS.md/CLAUDE.md
    memory files (global + repo hierarchy) are re-read on every call so
    edits take effect on the next turn (IMPROVEMENTS.md A3). The skills
    index (IMPROVEMENTS.md G5) is appended the same way — one line per
    discovered skill, only when the ``skill`` tool is in ``tools`` — so the
    model knows what it can load on demand without paying for the bodies.
    Shared by the REPL and the one-shot mode.
    """
    has_mcp = any(t.name.startswith("mcp_") for t in tools)
    mcp_note = " MCP tools (names prefixed 'mcp_') are also available."
    if not has_mcp:
        mcp_note = ""
    tool_names = ", ".join(sorted(t.name for t in tools))
    local_time, tz_label = _local_time_info()

    memory = load_agents_md(
        max_tokens=agents_md_max_tokens or _AGENTS_MD_MAX_TOKENS_DEFAULT
    )
    memory_block = ""
    if memory:
        memory_block = _MEMORY_BLOCK_TEMPLATE.format(content=memory)

    # Skills index (G5): one line per skill, only advertised when the
    # ``skill`` tool is actually in the registry — otherwise the model
    # would be told to call a tool it does not have. Like the tool list,
    # the index is stable for the session, so it stays cache-friendly.
    skills_block = ""
    if any(t.name == "skill" for t in tools):
        skills_block = render_skill_index(
            discover_skills(),
            max_tokens=skills_max_tokens or _SKILLS_MAX_TOKENS_DEFAULT,
        )

    return _SYSTEM_PROMPT_TEMPLATE.format(
        cwd=Path.cwd(),
        so=sys.platform,
        time=f"{local_time} Current timezone is: {tz_label}",
        tools=tool_names,
        mcp_note=mcp_note,
        skills_block=skills_block,
        memory_block=memory_block,
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
