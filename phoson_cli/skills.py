"""Skills — on-demand instruction packages (IMPROVEMENTS.md G5, issue #52).

A **skill** is a directory with a ``SKILL.md`` file: YAML-ish frontmatter
(``name`` + ``description``) followed by Markdown instructions, optionally
next to bundled resources (``scripts/``, ``references/``, templates…).
It is a third abstraction, distinct from the two the repo already has:

- **Plugins** (``phoson_agent/plugin.py``) — engine-level lifecycle hooks,
  always loaded once configured.
- **Tools** (``phoson_cli/tools/``) — function calls the model makes, whose
  schemas sit in *every* request.
- **Skills** — instructions the model pulls into context *only when
  relevant*, at the cost of one line in the system prompt while dormant.

**Progressive disclosure** is the whole point, and it is what makes skills
compatible with prompt caching (G2). Two tiers:

1. *Index* (this module's :func:`render_skill_index`): ``name: description``
   for every discovered skill, injected into the **stable prefix** of the
   system prompt. Cheap (one line each) and constant across turns, so the
   provider's prompt cache still covers the whole prefix.
2. *Body* (:func:`load_skill_body`): the full ``SKILL.md`` text, returned by
   the ``skill`` tool as a normal tool result — it lands in the *conversation*,
   never in the prefix, so loading a skill mid-session cannot invalidate the
   cached prompt.

**Where skills live** (all scanned; first match by name wins):

1. ``<repo>/.phoson/skills/`` — project skills, git-versionable.
2. ``<repo>/.agents/skills/`` and ``<repo>/.claude/skills/`` — read for
   compatibility with repos already set up for other agent harnesses (the
   same reason ``agents_md.py`` reads ``CLAUDE.md``).
3. ``~/.phoson/skills/`` — user-global skills, available in every repo.

Project skills shadow global ones with the same name, mirroring the
``AGENTS.md`` precedence rule ("closer to cwd is more specific"). Symlinked
duplicates (``.claude/skills/x -> ../../.agents/skills/x`` is a common
layout) are collapsed by resolved path, so the same skill is never listed
twice.

The module is deliberately **UI-independent and dependency-free** (no YAML
package, no prompt_toolkit, no Rich): the frontmatter subset used by skills
is a flat ``key: value`` table, which a ~40-line parser handles, and both
front ends plus the ``skill`` tool share the result.
"""

import logging
from typing import Final
from pathlib import Path
from dataclasses import dataclass

_LOGGER = logging.getLogger("phoson_cli.skills")

#: The manifest file that marks a directory as a skill.
SKILL_FILE: Final[str] = "SKILL.md"

#: Skills directories, relative to the repo root, in precedence order.
#: ``.agents``/``.claude`` are read-only compatibility locations.
PROJECT_SKILL_DIRS: Final[tuple[str, ...]] = (
    ".phoson/skills",
    ".agents/skills",
    ".claude/skills",
)

#: Default user-global skills directory.
DEFAULT_USER_SKILLS_DIR: Final[Path] = Path("~/.phoson/skills")

#: How deep under a skills root a ``SKILL.md`` may sit. Depth 1 is the
#: canonical ``skills/<name>/SKILL.md``; deeper levels support the nested
#: layouts marketplaces publish (``skills/<pack>/skills/<name>/SKILL.md``).
MAX_SCAN_DEPTH: Final[int] = 4

#: Hard cap on discovered skills, so a mistakenly huge directory cannot
#: flood the index (or the walk).
MAX_SKILLS: Final[int] = 64

#: Per-skill description cap (characters) inside the index.
MAX_DESCRIPTION_CHARS: Final[int] = 500

#: Budget for the whole index block injected into the system prompt.
DEFAULT_INDEX_MAX_TOKENS: Final[int] = 1000

#: Characters per token heuristic, same as ``agents_md.py``.
_CHARS_PER_TOKEN: Final[int] = 4

#: Cap on the body returned by the ``skill`` tool. Beyond this the text is
#: truncated with a pointer to the file, so ``read_file`` can page the rest.
MAX_BODY_CHARS: Final[int] = 32_000

#: Cap on how many bundled resource paths are listed with the body.
MAX_RESOURCE_ENTRIES: Final[int] = 40

#: Directory names never walked when listing a skill's resources.
_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", "node_modules", ".venv"}
)


@dataclass(frozen=True)
class SkillMeta:
    """A discovered skill.

    Attributes:
        name: Skill id used by the ``skill`` tool (frontmatter ``name``,
            falling back to the directory/file name).
        description: When to use it — the only thing the model sees before
            deciding to load the skill, so it carries the trigger wording.
        path: The ``SKILL.md`` file itself.
        root: Directory holding the skill (bundled resources live here).
        source: Which location it came from (``.phoson/skills``,
            ``~/.phoson/skills``, …) — shown by ``/skills``.
    """

    name: str
    description: str
    path: Path
    root: Path
    source: str


def _read_text(path: Path) -> str | None:
    """Read a file's text, or None when missing/unreadable/not UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _strip_quotes(value: str) -> str:
    """Drop one layer of matching quotes from a frontmatter value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``SKILL.md`` text into ``(frontmatter, body)``.

    Handles the flat subset skills actually use: ``key: value`` lines
    between ``---`` fences, single/double quotes, and YAML folded
    continuations (an indented line continues the previous value — how a
    long ``description`` is usually wrapped). Nested mappings and lists are
    ignored rather than rejected: an unknown extra key must never make a
    skill undiscoverable.

    Without a leading ``---`` fence the whole text is the body and the
    frontmatter is empty.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    data: dict[str, str] = {}
    last_key: str | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[index + 1 :])
            return data, body.lstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # A folded continuation: indented and not a "key:" line of its own.
        stripped = line.strip()
        is_indented = line[:1] in {" ", "\t"}
        head, sep, tail = stripped.partition(":")
        if is_indented and (not sep or " " in head):
            if last_key is not None:
                data[last_key] = f"{data[last_key]} {stripped}".strip()
            continue
        if not sep:
            continue
        key = head.strip().lower()
        if not key:
            continue
        value = _strip_quotes(tail.strip())
        data[key] = value
        last_key = key

    # Unterminated frontmatter: treat the file as pure body, since we
    # cannot tell where the instructions start.
    return {}, text


def _fallback_description(body: str) -> str:
    """First prose paragraph of a body, used when frontmatter has none."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "---", "```", ">", "|")):
            continue
        return line
    return ""


def _load_skill(path: Path, source: str) -> SkillMeta | None:
    """Build a :class:`SkillMeta` from a ``SKILL.md`` path (None if unusable)."""
    text = _read_text(path)
    if text is None:
        _LOGGER.warning("Skipping unreadable skill file %s", path)
        return None

    front, body = parse_frontmatter(text)
    root = path.parent
    default_name = root.name if path.name == SKILL_FILE else path.stem
    name = (front.get("name") or default_name).strip()
    if not name:
        return None
    description = (front.get("description") or _fallback_description(body)).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"

    return SkillMeta(
        name=name,
        description=description,
        path=path,
        root=root,
        source=source,
    )


def _iter_skill_files(root: Path, depth: int = 0) -> list[Path]:
    """Find ``SKILL.md`` manifests under *root*.

    A skill is *only* a directory containing ``SKILL.md``. Loose ``.md``
    files are deliberately not treated as single-file skills: a skills
    directory routinely holds a ``README.md`` (or notes), and silently
    indexing those as skills would put junk descriptions in front of the
    model — the explicit manifest is what makes discovery unambiguous.

    Bounded by :data:`MAX_SCAN_DEPTH` so a deep tree cannot stall startup.
    A directory containing ``SKILL.md`` is a leaf: its subdirectories are
    bundled resources, not nested skills.
    """
    if depth > MAX_SCAN_DEPTH:
        return []
    manifest = root / SKILL_FILE
    if manifest.is_file():
        return [manifest]

    found: list[Path] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if entry.is_dir():
            found.extend(_iter_skill_files(entry, depth + 1))
    return found


def _resolve_repo_root(cwd: Path) -> Path:
    """Closest ancestor containing ``.git`` (or ``cwd``). Mirrors agents_md."""
    current = cwd.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def skill_search_paths(
    cwd: Path | None = None,
    user_dir: Path | None = None,
) -> list[tuple[Path, str]]:
    """Return ``(directory, source_label)`` pairs to scan, in precedence order.

    Project directories come first (they shadow same-named global skills),
    then the user-global one. Missing directories are kept out of the list
    so callers can show exactly what was scanned.
    """
    workdir = (cwd or Path.cwd()).resolve()
    root = _resolve_repo_root(workdir)
    home = (user_dir or DEFAULT_USER_SKILLS_DIR).expanduser()

    candidates: list[tuple[Path, str]] = [
        (root / relative, relative) for relative in PROJECT_SKILL_DIRS
    ]
    candidates.append((home, "~/.phoson/skills"))
    return [(path, label) for path, label in candidates if path.is_dir()]


def discover_skills(
    cwd: Path | None = None,
    user_dir: Path | None = None,
) -> list[SkillMeta]:
    """Discover every available skill, deduplicated and sorted by name.

    Deduplication is two-layered:

    - by **resolved path**, which collapses the symlinked mirrors real
      repos have (``.claude/skills/x -> ../../.agents/skills/x``);
    - by **name**, where the first location in
      :func:`skill_search_paths` wins, so a project skill can override a
      user-global one deliberately.

    Never raises: an unreadable directory or a malformed ``SKILL.md`` is
    logged and skipped. Discovery is re-run on demand (it is a handful of
    ``stat`` calls) so adding a skill takes effect on the next turn without
    restarting the CLI.
    """
    by_name: dict[str, SkillMeta] = {}
    seen_paths: set[Path] = set()

    for directory, label in skill_search_paths(cwd=cwd, user_dir=user_dir):
        for path in _iter_skill_files(directory):
            try:
                resolved = path.resolve()
            except OSError:  # pragma: no cover - defensive
                resolved = path
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            skill = _load_skill(path, label)
            if skill is None:
                continue
            if skill.name in by_name:
                continue  # earlier (higher-precedence) location wins
            by_name[skill.name] = skill
            if len(by_name) >= MAX_SKILLS:
                _LOGGER.warning(
                    "Skill limit reached (%d); ignoring the rest", MAX_SKILLS
                )
                return sorted(by_name.values(), key=lambda s: s.name)

    return sorted(by_name.values(), key=lambda s: s.name)


def find_skill(name: str, skills: list[SkillMeta]) -> SkillMeta | None:
    """Resolve a skill by name: exact, then case-insensitive, then prefix.

    The forgiving lookup exists because the *model* types this name from
    the index; a case slip or a truncated id should still hit. An ambiguous
    prefix resolves to nothing (the caller lists the options instead of
    guessing).
    """
    for skill in skills:
        if skill.name == name:
            return skill
    lowered = name.strip().lower()
    matches = [s for s in skills if s.name.lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    matches = (
        [s for s in skills if s.name.lower().startswith(lowered)] if lowered else []
    )
    if len(matches) == 1:
        return matches[0]
    return None


def iter_skill_resources(skill: SkillMeta) -> list[str]:
    """Bundled files next to ``SKILL.md``, as paths relative to the skill root.

    These are what the instructions reference (``scripts/foo.py``,
    ``references/patterns.md``); the model reaches them with ``read_file``
    or ``bash``, so no new tool is needed to make a skill executable.
    """
    resources: list[str] = []
    for path in sorted(skill.root.rglob("*")):
        if len(resources) >= MAX_RESOURCE_ENTRIES:
            resources.append("…")
            break
        if path.name == SKILL_FILE and path.parent == skill.root:
            continue
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(skill.root)
        except ValueError:  # pragma: no cover - rglob stays under root
            continue
        # Only the *relative* parts may be filtered: the skill root itself
        # legitimately lives under a dot directory (``.phoson/skills/…``),
        # so testing the absolute parts would discard every resource.
        if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts):
            continue
        resources.append(str(relative))
    return resources


#: Framing for the index injected into the system prompt.
_INDEX_TEMPLATE: Final[str] = (
    "\n\n# Skills (load on demand)"
    "\nEach skill below is a package of instructions available on request."
    " Read the descriptions; when one matches the task, call the `skill`"
    " tool with its name to load the full instructions *before* doing the"
    " work. Do not guess a skill's contents from its description.\n\n"
    "{entries}"
)


def render_skill_index(
    skills: list[SkillMeta],
    max_tokens: int = DEFAULT_INDEX_MAX_TOKENS,
) -> str:
    """Render the system-prompt index block (``""`` when there are no skills).

    One line per skill (``- name: description``) inside a short framing
    that tells the model how to load one. Capped at *max_tokens*: entries
    are dropped from the end (never truncated mid-line into a half
    sentence) and the omission is stated explicitly, so the model is never
    told about a skill it cannot see the trigger for.
    """
    if not skills:
        return ""

    char_budget = max_tokens * _CHARS_PER_TOKEN
    lines: list[str] = []
    used = 0
    for skill in skills:
        detail = skill.description or "(no description)"
        line = f"- {skill.name}: {detail}"
        if used + len(line) + 1 > char_budget and lines:
            omitted = len(skills) - len(lines)
            lines.append(
                f"[... {omitted} more skill(s) omitted from this index"
                f" ({max_tokens}-token budget); run /skills to list them ...]"
            )
            break
        lines.append(line)
        used += len(line) + 1

    return _INDEX_TEMPLATE.format(entries="\n".join(lines))


def load_skill_body(skill: SkillMeta, max_chars: int = MAX_BODY_CHARS) -> str:
    """Full instructions for *skill*, framed for a tool result.

    Returns the ``SKILL.md`` body (frontmatter stripped — the model already
    saw ``name``/``description`` in the index), the skill's absolute root so
    bundled scripts can be run with ``bash`` without guessing the cwd, and
    the resource listing. Truncated at *max_chars* with a pointer to the
    file, so an oversized skill degrades into "read the rest yourself"
    instead of blowing up the context.
    """
    text = _read_text(skill.path)
    if text is None:
        return f"Skill {skill.name!r} could not be read at {skill.path}"

    _front, body = parse_frontmatter(text)
    body = body.strip()
    if len(body) > max_chars:
        body = (
            body[:max_chars].rsplit("\n", 1)[0]
            + f"\n\n[... truncated at {max_chars} characters."
            f" Read the rest with read_file on {skill.path} ...]"
        )

    header = [
        f"# Skill: {skill.name}",
        f"Location: {skill.root}",
    ]
    resources = iter_skill_resources(skill)
    if resources:
        header.append(
            "Bundled files (paths are relative to Location; use read_file or"
            " bash with the absolute path): " + ", ".join(resources)
        )
    header.append(
        "Follow these instructions for the current task; they take precedence"
        " over your defaults where they conflict."
    )

    return "\n".join(header) + "\n\n" + body


__all__ = [
    "DEFAULT_INDEX_MAX_TOKENS",
    "DEFAULT_USER_SKILLS_DIR",
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_SKILLS",
    "PROJECT_SKILL_DIRS",
    "SKILL_FILE",
    "SkillMeta",
    "discover_skills",
    "find_skill",
    "iter_skill_resources",
    "load_skill_body",
    "parse_frontmatter",
    "render_skill_index",
    "skill_search_paths",
]
