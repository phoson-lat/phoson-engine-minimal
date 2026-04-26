from phoson_agent import AgentTool

from .bash import bash
from .files import list_dir, read_file, write_file
from .search import web_search


def build_tools() -> list[AgentTool]:
    return [read_file, write_file, list_dir, bash, web_search]


__all__ = ["build_tools", "read_file", "write_file", "list_dir", "bash", "web_search"]
