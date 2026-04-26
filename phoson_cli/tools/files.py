import os
from pathlib import Path

from phoson_agent.tool import tool

MAX_BYTES = 50 * 1024
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


@tool
def read_file(path: str) -> str:
    """Read the contents of a file. path is relative to cwd."""
    file_path = Path(path)
    data = file_path.read_bytes()
    if len(data) > MAX_BYTES:
        text = data[:MAX_BYTES].decode("utf-8", errors="replace")
        return text + "\n\n[...truncated: file is larger than 50KB]"
    return data.decode("utf-8", errors="replace")


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file. Creates parent dirs if needed."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    file_path.write_bytes(encoded)
    return f"Written: {path} ({len(encoded)} bytes)"


@tool
def list_dir(path: str = ".") -> str:
    """List directory contents as a tree (max 3 levels deep)."""
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
        if rel_depth >= 3:
            dirs[:] = []

        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files = sorted(files)

        for dirname in dirs:
            lines.append(f"{'  ' * (rel_depth + 1)}{dirname}/")
        for filename in files:
            lines.append(f"{'  ' * (rel_depth + 1)}{filename}")
    return "\n".join(lines)
