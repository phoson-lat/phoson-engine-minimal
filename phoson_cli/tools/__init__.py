from phoson_agent import AgentTool

from .bash import bash
from .files import list_dir, read_file, write_file
from .search import web_search
from .subagent import agent, agents


def build_tools() -> list[AgentTool]:
    return [read_file, write_file, list_dir, bash, web_search, agent, agents]


def build_tools_dict() -> dict[str, AgentTool]:
    """Build tools as a dictionary for sub-agent lookup."""
    return {tool.name: tool for tool in build_tools()}


__all__ = [
    "build_tools",
    "build_tools_dict",
    "read_file",
    "write_file",
    "list_dir",
    "bash",
    "web_search",
    "agent",
    "agents",
]