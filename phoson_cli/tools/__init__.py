from phoson_agent import AgentTool

from .bash import bash
from .files import list_dir, read_file, patch_file, write_file
from .skill import skill
from .search import glob, grep
from .subagent import agent, agents
from .web_fetch import web_fetch
from .view_image import view_image
from .web_search import web_search

#: Tools always present, in prompt/registry order.
_BASE_TOOLS: list[AgentTool] = [
    read_file,
    write_file,
    patch_file,
    list_dir,
    view_image,
    bash,
    grep,
    glob,
    web_search,
    web_fetch,
    agent,
    agents,
]


def _has_skills() -> bool:
    """Whether any skill is discoverable right now (never raises)."""
    from phoson_cli.skills import discover_skills

    try:
        return bool(discover_skills())
    except Exception:  # noqa: BLE001 - discovery must never break tool building
        return False


def build_tools(include_skill: bool | None = None) -> list[AgentTool]:
    """Build the tool registry.

    Args:
        include_skill: Whether to expose the ``skill`` tool (G5). ``None``
            (default) auto-detects: it joins the registry only when at
            least one skill is discoverable. A schema the model can never
            use successfully is pure prompt cost on *every* request —
            skills exist precisely to avoid that kind of always-on
            overhead, so the activation tool follows the same rule. The
            flag makes the behaviour explicit for tests and callers.
    """
    tools = list(_BASE_TOOLS)
    if include_skill is None:
        include_skill = _has_skills()
    if include_skill:
        # Right after bash: grouped with the "act on this repo" tools rather
        # than appended after the subagent pair.
        tools.insert(tools.index(bash) + 1, skill)
    return tools


def build_tools_dict(include_skill: bool | None = None) -> dict[str, AgentTool]:
    """Build tools as a dictionary for sub-agent lookup."""
    return {tool.name: tool for tool in build_tools(include_skill=include_skill)}


__all__ = [
    "build_tools",
    "build_tools_dict",
    "read_file",
    "write_file",
    "patch_file",
    "list_dir",
    "view_image",
    "bash",
    "grep",
    "glob",
    "skill",
    "web_search",
    "web_fetch",
    "agent",
    "agents",
]
