"""File system manipulation tools.

The handlers are intentionally synchronous: filesystem reads and writes
on local files are fast enough that wrapping them in
``asyncio.to_thread`` would only add overhead. The agent's tool runner
already accepts sync handlers.

Edit contract (F-20 / #179, F-21a / #180):

- ``read_file`` renders ``cat -n`` output (1-based line numbers) so the
  model can anchor edits; the numbers are display-only and are **not**
  part of the file content.
- ``patch_file`` requires an *exact, unique* anchor: with
  ``replace_all=False`` a needle that occurs more than once is an error
  (with the matching line numbers) and nothing is written. A zero-match
  anchor gets a closest-line hint (difflib) plus a CRLF/LF note, which is
  the usual silent mismatch.
"""

import os
import difflib
from pathlib import Path

from phoson_agent.tool import tool

MAX_BYTES = 50 * 1024
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
#: Hard cap on entries rendered by ``list_dir`` (F-21b): a huge node_modules
#: subtree must not be able to flood the context.
LIST_DIR_MAX_ENTRIES = 500

#: Read slice cap in *bytes of rendered output* — applies to full reads and
#: to ranges alike (F-21a: ranges used to be unbounded).
_READ_MAX_BYTES = MAX_BYTES

#: How many occurrence line-numbers the ambiguity error lists before "…".
_MAX_REPORTED_OCCURRENCES = 10


def _not_utf8_message(path: str) -> str:
    """Actionable error for a file that is not valid UTF-8 (F-26)."""
    return (
        f"File is not valid UTF-8: {path}. Read it as text via bash "
        "(iconv -f <encoding> -t utf-8) or treat it as binary; do not "
        "guess its contents."
    )


def _numbered(lines: list[str], start: int) -> list[str]:
    """Render ``cat -n`` style lines: 6-wide right-aligned number + tab."""
    return [f"{n:>6}\t{line}" for n, line in enumerate(lines, start=start)]


def _cap_lines(numbered: list[str], start: int, end: int, total: int) -> str | None:
    """Truncate numbered output at ``_READ_MAX_BYTES`` with a next-range hint.

    Returns None when the output fits; otherwise the truncated text plus a
    note telling the model exactly which range to request next.
    """
    out = "\n".join(numbered)
    if len(out.encode("utf-8")) <= _READ_MAX_BYTES:
        return None

    budget = _READ_MAX_BYTES
    kept: list[str] = []
    used = 0  # running byte count of "\n".join(kept) (avoids O(n²) re-encode)
    for line in numbered:
        add = len(line.encode("utf-8")) + (1 if kept else 0)
        if used + add > budget:
            break
        kept.append(line)
        used += add
    if not kept:  # a single line is larger than the whole budget: hard-clip
        kept = [numbered[0][:budget]]

    last_shown = start + len(kept) - 1
    return (
        "\n".join(kept)
        + f"\n\n[...truncated: showing lines {start}-{last_shown} of {total} "
        f"({_READ_MAX_BYTES // 1024}KB cap). Read the rest with "
        f"start_line={last_shown + 1}, end_line={end}]"
    )


def _read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return f"File not found: {path}"

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _not_utf8_message(path)

    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        return f"(empty file: {path})"

    start = start_line if start_line is not None else 1
    end = end_line if end_line is not None else total
    if start > total:
        return f"start_line {start_line} is beyond file length ({total} lines)"
    start = max(1, start)
    end = min(total, max(end, start))

    numbered = _numbered(lines[start - 1 : end], start)
    capped = _cap_lines(numbered, start, end, total)
    return capped if capped is not None else "\n".join(numbered)


def _write_file(path: str, content: str) -> str:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    existed = file_path.exists()
    encoded = content.encode("utf-8")
    file_path.write_bytes(encoded)
    # "created" vs "updated" is part of the contract: the tool card
    # (formatting._write_summary_body) parses the verb out of this string,
    # so the wording stays stable (T-7).
    verb = "updated" if existed else "created"
    return f"{verb.capitalize()}: {path} ({len(encoded)} bytes)"


def _occurrence_lines(content: str, needle: str) -> list[int]:
    """1-based line numbers of every (non-overlapping) occurrence."""
    found: list[int] = []
    pos = 0
    while (idx := content.find(needle, pos)) != -1:
        found.append(content.count("\n", 0, idx) + 1)
        pos = idx + max(len(needle), 1)
    return found


def _dominant_eol(content: str) -> str:
    """The file's line ending ("\r\n" when it uses CRLF, else "\n")."""
    return "\r\n" if "\r\n" in content else "\n"


def _match_anchor(content: str, old_content: str) -> str | None:
    """Return the needle as it actually occurs in ``content``, or None.

    First tries the needle verbatim. When that fails, retries with the
    needle's line endings rewritten to the file's dominant ending, so an
    anchor copied from the (LF-normalised) read_file output still matches a
    CRLF file — the classic invisible mismatch. The returned needle is used
    for both counting and replacing.
    """
    if old_content in content:
        return old_content
    eol = _dominant_eol(content)
    candidate = old_content.replace("\n", eol)
    if candidate != old_content and candidate in content:
        return candidate
    # The reverse: an anchor typed with CRLF against an LF file.
    if eol == "\n" and old_content.replace("\r\n", "\n") in content:
        return old_content.replace("\r\n", "\n")
    return None


def _not_found_hint(content: str, old_content: str) -> str:
    """Help text for a zero-match anchor: closest line + CRLF/LF note.

    The two classic causes of a "not found" that the model cannot see are
    whitespace drift and a CRLF-vs-LF mismatch, so the hint targets exactly
    those: a difflib closest-line from the file, and an explicit note when
    the needle matches once the line endings are normalised.
    """
    hints: list[str] = []
    if old_content.replace("\r\n", "\n") in content.replace("\r\n", "\n"):
        hints.append(
            "it matches after normalising line endings — the file and "
            "old_content disagree on CRLF vs LF; copy the anchor from the "
            "read_file output instead of retyping it"
        )
    file_lines = content.splitlines()
    if file_lines:
        probe = old_content.splitlines()
        for candidate_line in probe:
            close = difflib.get_close_matches(
                candidate_line, file_lines, n=1, cutoff=0.8
            )
            if close:
                line_no = file_lines.index(close[0]) + 1
                hints.append(f"closest line in the file is {line_no}: {close[0]!r}")
                break
    if not hints:
        return ""
    return " " + "; ".join(hints) + "."


def _patch_file(
    path: str, old_content: str, new_content: str, replace_all: bool = False
) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return f"File not found: {path}"

    # Decode the raw bytes (no universal-newline translation): `read_text`
    # would normalise CRLF→LF in memory and writing back would silently
    # re-encode the whole file to LF. Preserving the file's own endings keeps
    # a targeted edit from changing every line ending.
    try:
        content = file_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return _not_utf8_message(path)

    # Match the anchor as it actually occurs in the file, absorbing a
    # CRLF/LF mismatch transparently (the needle is normalised to the
    # file's dominant line ending) instead of failing on an invisible
    # whitespace/ending difference.
    needle = _match_anchor(content, old_content)
    if needle is None:
        return (
            f"old_content not found in {path}.{_not_found_hint(content, old_content)}"
        )

    count = content.count(needle)

    if not replace_all and count > 1:
        # F-20/#179: an ambiguous anchor must fail loudly instead of
        # silently editing the first occurrence. Nothing is written.
        where = _occurrence_lines(content, needle)
        shown = ", ".join(str(n) for n in where[:_MAX_REPORTED_OCCURRENCES])
        if len(where) > _MAX_REPORTED_OCCURRENCES:
            shown += f" … (+{len(where) - _MAX_REPORTED_OCCURRENCES} more)"
        return (
            f"old_content matches {count} times in {path} (lines {shown}); "
            "refusing to guess which one. Re-read the file and extend "
            "old_content with surrounding context until it is unique, or "
            "pass replace_all=true if every occurrence should change."
        )

    # If the anchor was matched by re-encoding its line endings to the
    # file's dominant ending, do the same to the replacement so the edited
    # region blends with the surrounding lines (no mixed CRLF/LF block).
    replacement = new_content
    if needle is not old_content and "\n" in needle:
        replacement = new_content.replace("\n", _dominant_eol(content))

    new_content_full = (
        content.replace(needle, replacement)
        if replace_all
        else content.replace(needle, replacement, 1)
    )
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
    entries = 0
    truncated = False
    for current_root, dirs, files in os.walk(root):
        if truncated:
            break
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
            if entries >= LIST_DIR_MAX_ENTRIES:
                truncated = True
                break
            entries += 1
            lines.append(f"{'  ' * (rel_depth + 1)}{filename}")
    if truncated:
        lines.append(
            f"[… listing stopped at {LIST_DIR_MAX_ENTRIES} entries; "
            "narrow the path or use bash find/ls]"
        )
    return "\n".join(lines)


@tool
def read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    """Read a file with line numbers (cat -n) for anchoring edits.

    Use when you need the exact current content of a file — before editing
    it, or to answer a question about it. Prefer this over `bash cat`.
    Returns the file's lines prefixed with their 1-based line number
    (6-wide, tab-separated). The numbers are display-only: they are NOT
    part of the file content, so never include them in old_content when
    calling patch_file. Without start_line/end_line it returns the whole
    file, capped at 50KB — the truncation note names the exact next range
    to request. Use start_line/end_line (1-based, inclusive) to read a
    slice of a large file; slices are capped the same way. Do not use it
    to search many files — use the `grep` tool for that.
    """
    return _read_file(path, start_line, end_line)


@tool
def write_file(path: str, content: str) -> str:
    """Create a new file, or fully overwrite an existing one.

    Use to create a new file, or when rewriting most of an existing file
    (the whole new content is provided). For any targeted change in an
    existing file, prefer patch_file: it is smaller, diffable, and refuses
    to write when the anchor is ambiguous. Creates parent directories as
    needed. Returns 'Created' or 'Updated' plus the byte count written.
    """
    return _write_file(path, content)


@tool
def patch_file(
    path: str, old_content: str, new_content: str, replace_all: bool = False
) -> str:
    """Replace an exact, unique text block in a file (the preferred edit).

    Use for targeted changes to an existing file. old_content must match
    the file literally — same characters, whitespace, indentation and line
    endings — and, unless replace_all=true, it must occur exactly once: if
    it occurs more than once the call fails with the matching line numbers
    and writes nothing; re-read the file and extend the anchor with
    surrounding context until it is unique, then retry. If nothing
    matches, the error points at the closest line in the file and notes a
    CRLF-vs-LF mismatch when that is the cause — copy the anchor from the
    read_file output rather than retyping it. Use replace_all=true only
    when every occurrence should change. Returns the number of
    replacements made.
    """
    return _patch_file(path, old_content, new_content, replace_all)


@tool
def list_dir(path: str = ".") -> str:
    """List a directory tree (max 3 levels deep, max 500 entries).

    Use to orient yourself in a repository before reading or editing.
    Skips __pycache__, .git and node_modules. For filtered or deeper
    queries use bash with find/ls instead. Returns one entry per line,
    indented by depth.
    """
    return _list_dir(path)
