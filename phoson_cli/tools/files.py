"""File system manipulation tools.

The handlers are intentionally synchronous: filesystem reads and writes
on local files are fast enough that wrapping them in
``asyncio.to_thread`` would only add overhead. The agent's tool runner
already accepts sync handlers.
"""

import os
from pathlib import Path

from phoson_agent.tool import tool

MAX_BYTES = 50 * 1024
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


def _read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return f"File not found: {path}"

    content = file_path.read_text(encoding="utf-8")

    # No range specified — return full file with truncation guard.
    if start_line is None and end_line is None:
        if len(content.encode("utf-8")) > MAX_BYTES:
            return (
                content[:MAX_BYTES] + "\n\n[...truncated: file is larger than 50KB]"
            )
        return content

    lines = content.splitlines(keepends=True)

    # Convert to 0-indexed and clamp.
    start = (start_line - 1) if start_line else 0
    end = end_line if end_line else len(lines)
    start = max(0, min(start, len(lines)))
    end = max(0, min(end, len(lines)))

    if start >= len(lines):
        return f"start_line {start_line} is beyond file length ({len(lines)} lines)"

    return "".join(lines[start:end])


def _write_file(path: str, content: str) -> str:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    file_path.write_bytes(encoded)
    return f"Written: {path} ({len(encoded)} bytes)"


def _patch_file(
    path: str, old_content: str, new_content: str, replace_all: bool = False
) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return f"File not found: {path}"

    content = file_path.read_text(encoding="utf-8")

    if old_content not in content:
        return f"old_content not found in {path}"

    if replace_all:
        new_content_full = content.replace(old_content, new_content)
        count = content.count(old_content)
    else:
        new_content_full = content.replace(old_content, new_content, 1)
        count = 1

    encoded = new_content_full.encode("utf-8")
    file_path.write_bytes(encoded)

    return f"Replaced {count} occurrence(s) in {path} ({len(encoded)} bytes)"


def _list_dir(path: str = ".") -> str:
    root = Path(path)
    if not root.exists():
        return f"Path not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    lines = [f"{path}/"]
    base_depth = len(root.resolve().parts)
    for current_root, dirs, files in os.walk(root):
        current_path = Path(current_root)
        rel_depth = len(current_path.resolve().parts) - base_depth

        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if rel_depth >= 3:
            dirs[:] = []
            continue

        files = sorted(files)

        if rel_depth > 0:
            lines.append(f"{'  ' * rel_depth}{current_path.name}/")
        for filename in files:
            lines.append(f"{'  ' * (rel_depth + 1)}{filename}")
    return "\n".join(lines)


@tool
def read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    """Read the contents of a file, optionally between two line numbers."""
    return _read_file(path, start_line, end_line)


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file. Creates parent directories if needed."""
    return _write_file(path, content)


@tool
def patch_file(
    path: str, old_content: str, new_content: str, replace_all: bool = False
) -> str:
    """Replace ``old_content`` with ``new_content`` in a file."""
    return _patch_file(path, old_content, new_content, replace_all)


@tool
def list_dir(path: str = ".") -> str:
    """List directory contents as a tree (max 3 levels deep)."""
    return _list_dir(path)
