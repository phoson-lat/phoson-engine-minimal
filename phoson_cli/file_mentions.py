"""``@file`` mentions — turn ``@path`` tokens in a message into attached files.

Standard ``@``-mention pattern (Cursor / Claude Code): the user types
``@src/`` in the composer, the completer offers matching repo paths, and on
send the reference is *expanded* into the file's content (or a real media
block for images/audio/video/pdf) so the model sees the actual file, not just
its name.

This module is deliberately **UI-independent** — no prompt_toolkit, no Rich —
so the expansion lives in the controller (shared by the full-screen TUI and
the classic REPL) and the completer only *offers* candidates. Two concerns:

- :func:`expand_file_mentions` — pure parsing + resolution + block building.
  Given a message and a root directory it finds ``@mention`` tokens, resolves
  each against the root (relative → root, ``~/`` → home, absolute → as-is),
  and returns the resolved :class:`~phoson_llm.schemas.ContentBlock` sequence
  plus a per-mention status list the front end can use for feedback.
- :func:`iter_candidate_paths` — bounded walk of the working tree used by the
  inline path completer (skips ``.git``/``node_modules``/… and caps depth and
  entry count so typing never blocks on a huge repo).

Text files are inlined (with a head/tail cap so one giant file cannot blow
the context); images/audio/video/pdf become their native media blocks, the
same ones :class:`~phoson_cli.attachments.AttachmentManager` builds for
``/attach``.
"""

import re
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Iterator

from phoson_llm.schemas import (
    TextBlock,
    AudioBlock,
    ImageBlock,
    VideoBlock,
    ContentBlock,
    DocumentBlock,
)

from .attachments import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    MAX_ATTACHMENT_BYTES,
    _suffix_to_mime,
)

#: Cap on how many ``@mentions`` a single message may expand. Guards against
#: a pasted file listing (or a runaway model) fanning out into hundreds of
#: reads on one turn.
MAX_MENTIONS_PER_MESSAGE = 10

#: Cap on how many characters of a *text* file are inlined into the message.
#: Beyond this we keep a head/tail preview and point at the path, mirroring
#: the tool-output offload behavior. ~8k tokens at 4 chars/token.
MAX_INLINE_FILE_CHARS = 32_000

#: Head/tail kept when a text file exceeds :data:`MAX_INLINE_FILE_CHARS`.
_INLINE_HEAD_CHARS = 8_000
_INLINE_TAIL_CHARS = 2_000

#: Directories never descended into while offering path completions — VCS,
#: dependency, build and cache trees that would bury the real signal.
_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".cache",
        "dist",
        "build",
        "out",
        "target",
        ".idea",
        ".vscode",
        "coverage",
        ".tox",
    }
)

#: Depth bound for the completion walk (root = depth 0).
_WALK_MAX_DEPTH = 6
#: Entry bound for the completion walk (files + dirs counted together).
_WALK_MAX_ENTRIES = 2000

# A mention is ``@`` (not preceded by a word char, ``.``, ``-`` or ``@`` —
# so ``foo@bar.com`` is left alone) followed by a run of path-ish characters:
# letters, digits, ``.``, ``_``, ``-`` and ``/`` (subdirectories), optionally
# starting with ``~`` or ``./``. We capture the run, then trim trailing
# sentence punctuation and validate that it looks like a path (has a ``.``
# extension or a ``/``), which keeps bare ``@user`` out of scope.
_MENTION_TOKEN_RE = re.compile(r"(?<![\w.\-@])@([A-Za-z0-9._~/-]+)")
# Trailing punctuation that is sentence-ending, not part of a filename.
_TRAILING_PUNCT = ".!?;:,"


@dataclass
class FileMention:
    """One ``@mention`` found in a message, with its resolution status.

    Attributes:
        raw: The token exactly as typed, including the leading ``@``.
        path: The resolved absolute path (``Path``) the mention points at.
        ok: Whether the path exists and is a regular file.
        error: Human-readable reason when not :data:`ok` ("" when ok).
        kind: ``"text"`` or one of the media suffixes (".png", ...).
        block: The :class:`ContentBlock` this mention contributed, or ``None``
            when it could not be attached (missing / oversized / unsupported).
    """

    raw: str
    path: Path
    ok: bool = False
    error: str = ""
    kind: str = "text"
    block: ContentBlock | None = None


@dataclass
class ExpandResult:
    """Outcome of :func:`expand_file_mentions`.

    Attributes:
        blocks: Content blocks to fold into the user message, in order of
            appearance (text files first-class, media blocks as-is).
        mentions: One status entry per mention that was actually expanded
            (so the front end can report missing/oversized ones).
        truncated: True when the message named more mentions than
            :data:`MAX_MENTIONS_PER_MESSAGE` and the rest were left as text.
    """

    blocks: list[ContentBlock] = field(default_factory=list)
    mentions: list[FileMention] = field(default_factory=list)
    truncated: bool = False


def format_file_size(size: int) -> str:
    """Human-readable size (``12 B`` / ``3.4 KB`` / ``1.2 MB``) for UI hints.

    Pure and dependency-free so both the inline path completer (as a
    ``display_meta`` next to each candidate) and any other front end can
    reuse it without importing a UI layer.
    """
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _looks_like_path(token: str) -> bool:
    """Heuristic: does *token* read like a file path rather than ``@user``?

    Requires either a directory separator or a file-extension dot so that
    bare handles (``@team``) and domains in prose are not treated as files.
    """
    if "/" in token:
        return True
    # A dot that is not the first or last char (i.e. an extension).
    dot = token.find(".")
    return 0 < dot < len(token) - 1


def _resolve(token: str, root: Path) -> Path:
    """Resolve a mention *token* to an absolute :class:`Path`.

    Relative tokens are anchored at *root*; ``~/`` expands to home; absolute
    tokens are used as-is.
    """
    p = Path(token).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _inline_text_file(path: Path) -> str:
    """Read *path* as text for inlining, capping size with a head/tail cut."""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        data = ""
    header = f"[File: {path}]"
    if len(data) <= MAX_INLINE_FILE_CHARS:
        return f"{header}\n{data}"
    head = data[:_INLINE_HEAD_CHARS]
    tail = data[-_INLINE_TAIL_CHARS:]
    omitted = len(data) - len(head) - len(tail)
    return (
        f"{header} ({len(data)} chars — truncated, {omitted} omitted)\n"
        f"--- head ---\n{head}\n--- tail ---\n{tail}\n"
        f"[Read the full file at {path}]"
    )


def _build_block(path: Path, suffix: str) -> tuple[ContentBlock, str]:
    """Build the content block for a resolved *path*; returns (block, kind)."""
    if suffix in IMAGE_EXTS:
        return (
            ImageBlock(source=f"file://{path}", media_type=_suffix_to_mime(suffix)),
            suffix,
        )
    if suffix in AUDIO_EXTS:
        return (
            AudioBlock(source=f"file://{path}", format=suffix[1:]),
            suffix,
        )
    if suffix in VIDEO_EXTS:
        return (VideoBlock(source=f"file://{path}"), suffix)
    if suffix == ".pdf":
        return (DocumentBlock(source=f"file://{path}"), suffix)
    # Text (or any other readable) file: inline its contents.
    return (TextBlock(text=_inline_text_file(path)), "text")


def expand_file_mentions(
    text: str,
    cwd: Path | None = None,
) -> ExpandResult:
    """Find ``@mention`` tokens in *text* and build their content blocks.

    Args:
        text: The raw user message.
        cwd: The directory relative mentions are resolved against
            (defaults to the current working directory).

    Returns:
        An :class:`ExpandResult` with the resolved blocks (in order), one
        status entry per expanded mention, and a flag if the per-message
        cap was hit. Mentions that do not resolve are *kept* as literal text
        and reported (with a reason) rather than silently dropped — except
        bare tokens with no directory part (e.g. ``@john.smith``), which are
        almost certainly ``@user``/``@email`` handles in prose and are left
        as text without a warning.
    """
    root = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    result = ExpandResult()

    # De-duplicate by resolved path so "@a.py … @a.py" attaches once.
    seen: set[Path] = set()
    for m in _MENTION_TOKEN_RE.finditer(text):
        if len(result.mentions) >= MAX_MENTIONS_PER_MESSAGE:
            result.truncated = True
            break
        token = m.group(1).rstrip(_TRAILING_PUNCT)
        if not token or not _looks_like_path(token):
            continue
        path = _resolve(token, root)
        if path in seen:
            continue
        seen.add(path)

        mention = FileMention(raw="@" + token, path=path)
        if path.exists() and path.is_file():
            size = path.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                mention.error = (
                    f"file too large ({size / 1_048_576:.1f}MB, "
                    f"max {MAX_ATTACHMENT_BYTES / 1_048_576:.0f}MB)"
                )
            else:
                suffix = path.suffix.lower()
                block, kind = _build_block(path, suffix)
                mention.block = block
                mention.kind = kind
                mention.ok = True
                result.blocks.append(block)
        elif "/" in token:
            # Has a directory part, so the user clearly meant a path — a
            # real (broken) reference. Report it so it isn't lost.
            mention.error = (
                f"file not found: {path}"
                if not path.exists()
                else f"not a file: {path}"
            )
        else:
            # Bare token (no "/") that isn't a file: almost certainly an
            # @user / @email handle in prose, not a file — leave as text.
            continue

        result.mentions.append(mention)

    return result


def iter_candidate_paths(
    root: Path | None = None,
    *,
    max_depth: int = _WALK_MAX_DEPTH,
    max_entries: int = _WALK_MAX_ENTRIES,
) -> Iterator[str]:
    """Yield candidate relative paths for the ``@`` completer.

    Files yield their path relative to *root* (``/``-separated, as the user
    types them); directories yield a trailing ``/`` so completing them
    keeps navigating. Dot-files and the :data:`_IGNORED_DIR_NAMES` trees are
    skipped, and the walk stops at the depth / entry bounds so a large repo
    never blocks the input.
    """
    base = Path(root).resolve() if root is not None else Path.cwd().resolve()
    entries = 0
    yield_count = 0

    def _walk(directory: Path, rel: str, depth: int) -> Iterator[str]:
        nonlocal entries, yield_count
        if depth > max_depth:
            return
        try:
            items = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError:
            return
        for item in items:
            entries += 1
            if entries > max_entries:
                return
            name = item.name
            if name.startswith("."):
                continue
            sub_rel = f"{rel}{name}" if not rel else f"{rel}/{name}"
            if item.is_dir():
                if name in _IGNORED_DIR_NAMES:
                    continue
                yield sub_rel + "/"
                yield_count += 1
                if yield_count >= max_entries:
                    return
                yield from _walk(item, sub_rel, depth + 1)
            else:
                yield sub_rel
                yield_count += 1
                if yield_count >= max_entries:
                    return

    yield from _walk(base, "", 0)


__all__ = [
    "FileMention",
    "ExpandResult",
    "expand_file_mentions",
    "iter_candidate_paths",
    "format_file_size",
    "MAX_MENTIONS_PER_MESSAGE",
]
