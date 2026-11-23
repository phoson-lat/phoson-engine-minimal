"""Memory tools backed by the CLI's shared, atomic Markdown memory API."""

from typing import Any
from pathlib import Path

from phoson_cli import memory as memory_api
from phoson_agent.tool import tool


def memory_tools() -> list[Any]:
    """Create memory tools; project operations require an injected workspace."""

    def project(workspace: str | None) -> Path:
        if workspace is None or not str(workspace).strip():
            raise ValueError("memory tools require a workspace")
        return Path(workspace)

    @tool(parallel_safe=True, inject=["workspace"])
    def memory_list(scope: str = "project", workspace: str | None = None) -> list[str]:
        """List saved memory names in global or current project scope."""
        return [
            p.stem
            for p in memory_api.list_memories(
                scope, project=project(workspace) if scope == "project" else None
            )
        ]

    @tool(parallel_safe=True, inject=["workspace"])
    def memory_read(
        name: str, scope: str = "project", workspace: str | None = None
    ) -> str:
        """Read a memory by name from global or current project scope."""
        try:
            return memory_api.read_memory(
                name, scope, project=project(workspace) if scope == "project" else None
            )
        except FileNotFoundError:
            return "Memory not found."

    @tool(inject=["workspace"])
    def memory_write(
        name: str, content: str, scope: str = "project", workspace: str | None = None
    ) -> str:
        """Save non-secret durable information to memory. Never store credentials."""
        memory_api.write_memory(
            name,
            content,
            scope,
            project=project(workspace) if scope == "project" else None,
        )
        return f"Saved memory '{name}' ({scope})."

    @tool(inject=["workspace"])
    def memory_delete(
        name: str, scope: str = "project", workspace: str | None = None
    ) -> str:
        """Delete a memory by name from global or current project scope."""
        try:
            memory_api.delete_memory(
                name, scope, project=project(workspace) if scope == "project" else None
            )
            return f"Deleted memory '{name}'."
        except FileNotFoundError:
            return "Memory not found."

    return [memory_list, memory_read, memory_write, memory_delete]
