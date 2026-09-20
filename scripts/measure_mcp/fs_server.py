"""Hermetic stdio MCP server mimicking @modelcontextprotocol/server-filesystem.

Used by ``scripts/measure_tool_definitions.py`` (issue #148) to measure the
*real* token weight of a typical MCP server catalog: real JSON schemas,
real tool discovery through the real MCP plugin (stdio transport).
"""

import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("filesystem")


@mcp.tool()
def read_file(path: str) -> str:
    """Reads a file from the file system."""
    return Path(path).read_text()


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Writes new content to a file. Overwrites if existing, creates if not."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} bytes to {path}"


@mcp.tool()
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Performs exact string replacement in a file. old_text must match exactly."""
    p = Path(path)
    text = p.read_text()
    if old_text not in text:
        return f"Error: old_text not found in {path}"
    p.write_text(text.replace(old_text, new_text, 1))
    return f"Edited {path}"


@mcp.tool()
def list_directory(path: str) -> str:
    """Lists all files in a directory, recursively."""
    lines = [str(p) for p in sorted(Path(path).rglob("*"))]
    return "\n".join(lines) or "(empty)"


@mcp.tool()
def directory_tree(path: str) -> str:
    """Get a recursive tree view of files and directories, prefixed with their type."""
    root = Path(path)

    def walk(p: Path, prefix: str) -> list[str]:
        out = []
        for entry in sorted(p.iterdir()):
            label = f"[dir] {entry.name}/" if entry.is_dir() else f"[file] {entry.name}"
            out.append(prefix + label)
            if entry.is_dir():
                out.extend(walk(entry, prefix + "  "))
        return out

    return "\n".join(walk(root, "")) or "(empty)"


@mcp.tool()
def search_files(directory: str, pattern: str) -> str:
    """Searches for files matching a glob pattern within a directory, recursively."""
    matches = [str(p) for p in Path(directory).rglob(pattern)]
    return "\n".join(matches) or "No matches found"


@mcp.tool()
def get_file_info(path: str) -> str:
    """Retrieves metadata about a file or directory (size, modified time, etc.)."""
    p = Path(path)
    stat = p.stat()
    kind = "directory" if p.is_dir() else "file"
    return f"Type: {kind}\nSize: {stat.st_size} bytes\nModified: {stat.st_mtime}"


@mcp.tool()
def move_file(source: str, destination: str) -> str:
    """Move or rename a file or directory."""
    shutil.move(source, destination)
    return f"Moved {source} -> {destination}"


if __name__ == "__main__":
    mcp.run()
