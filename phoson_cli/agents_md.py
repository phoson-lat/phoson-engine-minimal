"""AGENTS.md filesystem memory (IMPROVEMENTS.md A3).

Loads project/user instruction files into the system prompt so the agent
remembers repo conventions ("use ruff and pytest, never black") without
requiring a memory plugin backed by Redis/Postgres/Qdrant. The filesystem
is free, git-versionable, and visible to the user.

Loaded, in order of precedence (later files are appended after earlier
ones so closer-to-cwd instructions read as more specific):

1. ``~/.phoson/AGENTS.md`` — user-global instructions.
2. ``AGENTS.md`` / ``CLAUDE.md`` per directory from the repository root
   down to the working directory (hierarchical; ``CLAUDE.md`` is
   supported as an alias for compatibility with repos already configured
   for other tools).
3. ``@path/to/file.md`` imports inside any loaded file are expanded once
   (no cycles, missing imports skipped with a note). Imports are
   **confined** to the file's own tree — a project file cannot import
   outside the repository root, and the global ``~/.phoson/AGENTS.md``
   cannot import outside ``~/.phoson/`` — so a hostile project file
   cannot leak ``/etc/passwd`` or ``~/.ssh`` into the system prompt.

The result is capped at ``max_tokens`` (heuristic: ~4 characters per
token) and truncated with a visible marker when it exceeds the budget.
Files are re-read on every call so edits take effect on the next turn
(cache-busting); they are small by design.
"""

import logging
from pathlib import Path

_LOGGER = logging.getLogger("phoson_cli.agents_md")

#: Characters per token heuristic for capping the injected content.
_CHARS_PER_TOKEN = 4

#: Default budget for the combined AGENTS.md content (tokens).
DEFAULT_MAX_TOKENS = 2000

#: Marker appended when the budget forces a truncation.
_TRUNCATION_MARKER = (
    "\n\n[... AGENTS.md content truncated to fit the {max_tokens}-token budget ...]"
)

_MAX_IMPORT_DEPTH = 5


def _read_text(path: Path) -> str | None:
    """Read a file's text, or None when missing/unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_repo_root(cwd: Path) -> Path:
    """Walk up from ``cwd`` to find the repository root (or fall back to cwd).

    The root is the closest ancestor containing ``.git``. When none is
    found, ``cwd`` itself is used as the single scanned directory.
    """
    current = cwd.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def _expand_imports(
    text: str,
    base_dir: Path,
    seen: set[Path],
    depth: int = 0,
    root: Path | None = None,
    expand_home: bool = False,
) -> tuple[str, list[Path]]:
    """Expand ``@relative/path.md`` import lines.

    Returns ``(expanded_text, imported_paths)``. Imports are expanded once
    per file (cycles are broken via ``seen``), depth-limited, and missing
    targets are replaced by a short note instead of failing.

    ``root`` is the confinement tree: when given, every import target must
    resolve inside it. Targets escaping the tree (absolute paths, ``..``
    traversal, symlinks) are refused with a visible marker instead of being
    inlined, so a hostile file cannot drag ``/etc/passwd`` or ``~/.ssh``
    into the system prompt. ``expand_home`` (only for the global user file)
    lets a leading ``~`` be expanded before the confinement check.
    """
    imported: list[Path] = []
    if depth >= _MAX_IMPORT_DEPTH:
        return text, imported

    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("@") and len(stripped) > 1):
            out_lines.append(line)
            continue
        target_rel = stripped[1:].strip()
        try:
            if expand_home and target_rel.startswith("~"):
                target = Path(target_rel).expanduser()
            else:
                target = base_dir / target_rel
            target = target.resolve()
        except (OSError, RuntimeError):
            out_lines.append(f"[import refused: {target_rel}]")
            continue
        if root is not None and not target.is_relative_to(root):
            out_lines.append(f"[import refused: outside repo: {target_rel}]")
            continue
        if not target.is_file():
            out_lines.append(f"[import not found: {target_rel}]")
            continue
        if target in seen:
            out_lines.append(f"[import already included: {target_rel}]")
            continue
        seen.add(target)
        content = _read_text(target)
        if content is None:
            out_lines.append(f"[import unreadable: {target_rel}]")
            continue
        imported.append(target)
        nested, nested_imported = _expand_imports(
            content,
            target.parent,
            seen,
            depth + 1,
            root=root,
            expand_home=expand_home,
        )
        imported.extend(nested_imported)
        out_lines.append(nested)

    return "\n".join(out_lines), imported


def collect_agents_md_files(
    cwd: Path | None = None,
    home_file: Path | None = None,
) -> list[tuple[Path, str]]:
    """Collect the AGENTS.md/CLAUDE.md files that apply to ``cwd``.

    Returns ordered ``(path, raw_content)`` pairs:

    1. the user-global file (default ``~/.phoson/AGENTS.md``) when present;
    2. one entry per directory from the repo root down to ``cwd``, each
       reading ``AGENTS.md`` first and falling back to ``CLAUDE.md``.
    """
    workdir = (cwd or Path.cwd()).resolve()
    global_file = home_file or Path("~/.phoson/AGENTS.md").expanduser()

    collected: list[tuple[Path, str]] = []

    global_content = _read_text(global_file)
    if global_content is not None:
        collected.append((global_file, global_content))

    root = _resolve_repo_root(workdir)
    # Order matters: root first, cwd last (closest instructions last).
    directories: list[Path] = []
    cursor: Path | None = workdir
    while cursor is not None:
        try:
            if cursor.is_relative_to(root):
                directories.append(cursor)
        except ValueError:
            pass
        if cursor == root:
            break
        cursor = cursor.parent
    directories.reverse()

    for directory in directories:
        agents_path = directory / "AGENTS.md"
        claude_path = directory / "CLAUDE.md"
        content = _read_text(agents_path)
        if content is not None:
            collected.append((agents_path, content))
            continue
        content = _read_text(claude_path)
        if content is not None:
            collected.append((claude_path, content))

    return collected


def load_agents_md(
    max_tokens: int = DEFAULT_MAX_TOKENS,
    cwd: Path | None = None,
    home_file: Path | None = None,
) -> str:
    """Build the memory block to inject into the system prompt.

    Concatenates every applicable file (global, then root→cwd), expands
    ``@file`` imports, caps the total at ``max_tokens`` (truncating with a
    visible marker) and returns the block *without* any wrapper heading —
    callers decide how to frame it in the prompt. Returns "" when no file
    exists anywhere.
    """
    files = collect_agents_md_files(cwd=cwd, home_file=home_file)
    if not files:
        return ""

    # Per-file confinement trees: project files may only import inside the
    # repository root; the global user file only inside ``~/.phoson/`` (its
    # own directory) and may additionally use ``~``.
    workdir = (cwd or Path.cwd()).resolve()
    repo_root = _resolve_repo_root(workdir)
    global_path = (home_file or Path("~/.phoson/AGENTS.md").expanduser()).resolve()

    sections: list[str] = []
    seen: set[Path] = {path for path, _ in files}
    for path, raw in files:
        is_global = path.resolve() == global_path
        if is_global:
            containment, expand_home = path.resolve().parent, True
        else:
            containment, expand_home = repo_root, False
        expanded, _imports = _expand_imports(
            raw,
            path.parent,
            seen,
            root=containment,
            expand_home=expand_home,
        )
        label = (
            "~" + str(path).replace(str(Path.home()), "", 1)
            if _is_home(path)
            else str(path)
        )
        sections.append(f"### {label}\n{expanded.strip()}")

    combined = "\n\n".join(sections)

    char_budget = max_tokens * _CHARS_PER_TOKEN
    if len(combined) > char_budget:
        clipped = combined[:char_budget]
        # Avoid cutting mid-line: drop the partial tail line.
        clipped = clipped.rsplit("\n", 1)[0]
        combined = clipped + _TRUNCATION_MARKER.format(max_tokens=max_tokens)

    return combined


def _is_home(path: Path) -> bool:
    """Whether ``path`` lives directly under the user's home directory."""
    try:
        return path.resolve().is_relative_to(Path.home())
    except (OSError, ValueError):
        return False


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "collect_agents_md_files",
    "load_agents_md",
]
