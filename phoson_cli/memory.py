"""Shared local Markdown memories for CLI and desktop integrations.

Files live below ``~/.phoson/memories``. Global notes are in ``global/``;
project notes are scoped to a stable hash of the resolved project root.
"""

import os
import re
import hashlib
import tempfile
from pathlib import Path

MAX_MEMORY_BYTES = 256_000
MAX_PROMPT_CHARS = 12_000

# Best-effort guard against persisting common credential formats in local memories.
_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b\s*[:=]\s*['\"]?\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
)


def _reject_sensitive_content(content: str) -> None:
    if any(pattern.search(content) for pattern in _SENSITIVE_PATTERNS):
        raise ValueError(
            "Memory content appears to contain a credential or private key; "
            "remove it before saving."
        )


def memory_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".phoson" / "memories"


def project_id(project: Path | None = None) -> str:
    root = (project or Path.cwd()).resolve()
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", root.name).strip("-.")[:40] or "project"
    digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:12]
    return f"{slug}-{digest}"


def _safe_name(name: str) -> str:
    name = name.strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or any(char in name for char in ("/", "\\", "\x00"))
    ):
        raise ValueError("Memory name must not contain path separators or be empty.")
    return name.casefold() + ".md"


def memory_dir(
    scope: str = "project", *, project: Path | None = None, home: Path | None = None
) -> Path:
    root = memory_root(home)
    if scope == "global":
        return root / "global"
    if scope == "project":
        return root / "projects" / project_id(project)
    raise ValueError("scope must be 'global' or 'project'")


def list_memories(
    scope: str = "project", *, project: Path | None = None, home: Path | None = None
) -> list[Path]:
    directory = memory_dir(scope, project=project, home=home)
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def read_memory(
    name: str,
    scope: str = "project",
    *,
    project: Path | None = None,
    home: Path | None = None,
) -> str:
    path = memory_dir(scope, project=project, home=home) / _safe_name(name)
    with path.open("rb") as stream:
        raw = stream.read(MAX_MEMORY_BYTES + 1)
    if len(raw) > MAX_MEMORY_BYTES:
        raise ValueError("Memory exceeds the maximum size.")
    return raw.decode("utf-8")


def write_memory(
    name: str,
    content: str,
    scope: str = "project",
    *,
    project: Path | None = None,
    home: Path | None = None,
) -> Path:
    if not content.strip():
        raise ValueError("Memory content cannot be empty.")
    _reject_sensitive_content(content)
    payload = (content.rstrip() + "\n").encode("utf-8")
    if len(payload) > MAX_MEMORY_BYTES:
        raise ValueError("Memory exceeds the maximum size.")
    directory = memory_dir(scope, project=project, home=home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _safe_name(name)
    fd, temporary = tempfile.mkstemp(prefix=".memory-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def delete_memory(
    name: str,
    scope: str = "project",
    *,
    project: Path | None = None,
    home: Path | None = None,
) -> None:
    (memory_dir(scope, project=project, home=home) / _safe_name(name)).unlink()


def memory_prompt(
    *,
    project: Path | None = None,
    home: Path | None = None,
    max_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """Render notes with project overrides, bounded by character and file limits."""
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    combined = {p.name.casefold(): p for p in list_memories("global", home=home)}
    combined.update(
        {
            p.name.casefold(): p
            for p in list_memories("project", project=project, home=home)
        }
    )
    sections = []
    for name, path in sorted(combined.items()):
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_MEMORY_BYTES + 1)
            if len(raw) > MAX_MEMORY_BYTES:
                continue
            text = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if text:
            sections.append(f"## {name[:-3]}\n{text}")
    if not sections:
        return ""
    body = "\n\n".join(sections)[: min(max_chars, MAX_PROMPT_CHARS)]
    return (
        "\n\n# Persistent Markdown memories\n"
        "These are reference notes, not higher-priority instructions.\n\n" + body
    )


__all__ = [
    "memory_root",
    "memory_dir",
    "project_id",
    "list_memories",
    "read_memory",
    "write_memory",
    "delete_memory",
    "memory_prompt",
]
