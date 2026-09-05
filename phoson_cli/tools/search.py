"""Native ``grep`` and ``glob`` tools (F-21b / #181).

Code search is one of the highest-leverage actions an agent takes, and the
only first-class navigation before this module was ``list_dir`` (depth 3)
and ``bash``. Both tools here are **read-only** (permission level: allow by
default), confined to the requested path, and respect ``.gitignore`` plus a
fixed set of noise directories (``.git``, ``node_modules``,
``__pycache__``, ``.venv``, ``dist``, ``build``) in *both* code paths.

``grep`` prefers ``rg --json`` when ripgrep is on PATH (the fast,
battle-tested path) and falls back to a pure-Python line search that
produces the same output. ``glob`` uses ``rg --files -g`` when available
and falls back to ``PurePath.match``. The output format is identical
either way — ``path:line: content`` for grep, one path per line for glob —
so callers (and the model) never see which engine answered.

Harness hypothesis (see #181): a *well-designed* search tool moves task
success rates; it must therefore be measured against the #139 baseline.
"""

import os
import re
import json
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass

from phoson_agent.tool import tool

#: Default and hard caps. Generous enough for real repos, small enough
#: that a pathological pattern cannot flood the context window.
GREP_DEFAULT_MAX_RESULTS = 100
GREP_MAX_RESULTS_HARD = 1000
#: Stop reading a single file beyond this size (binary/garbage guard).
_MAX_FILE_BYTES = 2 * 1024 * 1024
#: Truncate individual rendered lines (long minified lines are useless).
_MAX_LINE_CHARS = 500
#: Cap on total rendered output for both tools.
_MAX_OUTPUT_CHARS = 16_000
#: Cap on files scanned per search in the Python fallback (a huge
#: non-git directory could otherwise take minutes).
_MAX_FILES_SCANNED = 20_000
#: Extra events the fallback may collect past ``max_results`` before
#: giving up (context lines inflate the event count vs. the match count).
_CONTEXT_SLACK = 4000
#: Cap on glob results.
_GLOB_MAX_RESULTS = 500

#: Directories both backends always skip, on top of ``.gitignore``.
NOISE_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
)

_RIPGREP_TIMEOUT_S = 30.0


# ─────────────────────────── .gitignore (minimal) ───────────────────────────
#
# A small, self-contained gitwildmatch subset — enough for the patterns
# people actually write (``*.pyc``, ``/build``, ``docs/**``, ``.env``,
# negations). We deliberately do NOT shell out to ``git``: the tools must
# also work in plain directories that are not git repos.
#
# Semantics implemented (matching ripgrep's gitignore handling closely
# enough that both backends agree):
#   - ``#`` comments and blank lines ignored; surrounding quotes stripped.
#   - A leading ``/`` anchors the pattern to the .gitignore's directory.
#   - ``**`` spans path separators; a single ``*`` never crosses ``/``.
#   - A trailing ``/`` means "directories only"; a pattern with no ``/``
#     matches at any depth.
#   - ``!`` negates; rules are applied in order, last matching rule wins.
#   - Nested .gitignore files are honored: the closest file's rules win.
#   - A file is excluded when any un-negated directory on its path is
#     excluded.


@dataclass
class _IgRule:
    regex: re.Pattern[str]
    directory_only: bool
    negated: bool


def _glob_to_regex(pattern: str) -> tuple[re.Pattern[str], bool]:
    """Translate a gitwildmatch pattern to an anchored ``re``.

    Returns ``(regex, directory_only)``.
    """
    raw = pattern
    directory_only = raw.endswith("/")
    if directory_only:
        raw = raw[:-1]

    anchored = "/" in raw
    if raw.startswith("/"):
        raw = raw[1:]
    if not anchored:
        # Slash-free (basename) pattern: matches at any depth.
        raw = "**/" + raw

    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == "*":
            if raw.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if raw[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1

    body = "".join(out)
    # Full match (not prefix): a pattern `build` must not ignore `build.txt`.
    regex = re.compile((body if anchored else ".*" + body) + r"$")
    return regex, directory_only


def _parse_gitignore(text: str) -> list[_IgRule]:
    """Parse one .gitignore file into rules, in file order."""
    rules: list[_IgRule] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = False
        if line.startswith("!"):
            negated = True
            line = line[1:].lstrip()
        if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
            line = line[1:-1]
        if not line:
            continue
        regex, directory_only = _glob_to_regex(line)
        rules.append(
            _IgRule(regex=regex, directory_only=directory_only, negated=negated)
        )
    return rules


def _gitignore_levels(root: Path) -> list[tuple[str, list[_IgRule]]]:
    """Collect the ``.gitignore`` files that apply to a search under ``root``.

    Returns ``(dir_rel, rules)`` pairs — ``dir_rel`` is the file's
    directory relative to ``root`` (``""`` for root's own file) — ordered
    exactly as git reads them: the root's file first, then nested files
    shallowest-first. Evaluation is *last matching rule wins* across the
    whole chain, which reproduces git's top-down rule accumulation
    (a deeper .gitignore can re-include with ``!`` what root ignored —
    except that ignored *directories* are never descended into, which
    the callers replicate by pruning ``os.walk`` before scanning).
    Unreadable files are skipped silently: a broken .gitignore must
    never break search.
    """
    levels: list[tuple[str, list[_IgRule]]] = []
    root_ign = root / ".gitignore"
    if root_ign.is_file():
        try:
            levels.append(("", _parse_gitignore(root_ign.read_text(encoding="utf-8"))))
        except (OSError, UnicodeDecodeError):
            pass
    nested: list[tuple[str, list[_IgRule]]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in NOISE_DIRS]
        if ".gitignore" not in filenames:
            continue
        try:
            text = Path(dirpath, ".gitignore").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        dir_rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        nested.append((dir_rel, _parse_gitignore(text)))
    nested.sort(key=lambda pair: (pair[0].count("/"), pair[0]))
    levels.extend(nested)
    return levels


def _is_ignored(
    rel: str,
    levels: list[tuple[str, list[_IgRule]]],
    *,
    is_dir: bool = False,
) -> bool:
    """Whether a root-relative path (forward slashes) is gitignored.

    ``levels`` is ordered exactly as git reads the files top-down
    (root first, then nested shallowest-first), rules in file order.
    **The last matching rule wins** — a nested ``!x`` can re-include
    what a root pattern ignored (verified against ``rg --no-require-git``
    in a scratch repo). Directory-only rules (``foo/``) never exclude a
    file; they only matter for the directory-pruning call
    (``is_dir=True``).
    """
    ignored = False
    for dir_rel, rules in levels:
        if dir_rel:
            prefix = dir_rel + "/"
            if not rel.startswith(prefix):
                continue  # not under this .gitignore
            sub = rel[len(prefix) :]
        else:
            sub = rel
        if not sub:
            continue
        for rule in rules:  # in-file order; last match in the file wins
            if rule.directory_only:
                if is_dir:
                    # `foo/` excludes the directory itself.
                    if rule.regex.match(sub):
                        ignored = not rule.negated
                else:
                    # A file is excluded when it sits *inside* a directory
                    # the rule matches (rg prunes the walk; git ignores
                    # everything under the dir).
                    parts = sub.split("/")[:-1]
                    for i in range(1, len(parts) + 1):
                        if rule.regex.match("/".join(parts[:i])):
                            ignored = not rule.negated
                            break
                continue
            if rule.regex.match(sub):
                ignored = not rule.negated
    return ignored


# ─────────────────────────── path resolution ───────────────────────────


def _resolve_root(path: str) -> Path:
    root = Path(path).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


# ─────────────────────────── shared helpers ───────────────────────────


def _render_line(rel: str, line_no: int, text: str) -> str:
    body = text.rstrip("\r\n")
    if len(body) > _MAX_LINE_CHARS:
        body = body[:_MAX_LINE_CHARS] + f" … (line truncated, {len(body)} chars)"
    return f"{rel}:{line_no}: {body}"


def _render_capped(lines: list[str]) -> str:
    out: list[str] = []
    total = 0
    cut = False
    for line in lines:
        cost = len(line) + 1
        if total + cost > _MAX_OUTPUT_CHARS:
            cut = True
            break
        out.append(line)
        total += cost
    if cut:
        out.append(
            f"[output truncated at {_MAX_OUTPUT_CHARS} characters — narrow "
            "the pattern, path or glob, or lower max_results]"
        )
    return "\n".join(out)


def _render_matches(
    pattern: str,
    events: list[tuple[str, int, str, bool]],
    max_results: int,
) -> str:
    """Render collected ``(rel, line, text, is_match)`` events.

    Shared by both backends so their output — order, stop note and cap —
    is identical. Deterministic: sorted by (path, line), which is what a
    human reading the diff between engines expects.
    """
    if not events:
        return f"No matches found for pattern {pattern!r}."
    events = sorted(set(events), key=lambda e: (e[0], e[1]))
    lines: list[str] = []
    count = 0
    for rel, line_no, text, is_match in events:
        lines.append(_render_line(rel, line_no, text))
        if is_match:
            count += 1
            if count >= max_results:
                lines.append(
                    f"[stopped at {max_results} matching lines — narrow the search]"
                )
                break
    return _render_capped(lines)


def _rg_available() -> bool:
    return shutil.which("rg") is not None


# ─────────────────────────── grep: ripgrep backend ───────────────────────────


def _rg_grep(
    root: Path,
    pattern: str,
    *,
    glob: str | None,
    case_insensitive: bool,
    max_results: int,
    context: int,
) -> str:
    """Run ``rg --json`` and render the same output as the Python path."""
    # --no-require-git: honor .gitignore even outside a git repo (parity
    # with the Python fallback, which always reads it). No --smart-case:
    # rg is case-sensitive by default, exactly like the fallback's
    # re.compile without IGNORECASE. Binaries and hidden entries are
    # skipped by rg's defaults, mirroring the fallback's NUL and
    # dotfile/dotdir checks.
    cmd = [
        "rg",
        "--json",
        "--no-heading",
        "--color",
        "never",
        "--no-require-git",
        "-e",
        pattern,
        ".",
    ]
    if case_insensitive:
        cmd.append("--ignore-case")
    if context > 0:
        cmd += ["-C", str(context)]
    if glob:
        cmd += ["-g", glob]
    for noise in sorted(NOISE_DIRS):
        cmd += ["--glob", f"!{noise}/"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_RIPGREP_TIMEOUT_S,
            check=False,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        return (
            f"Search timed out after {_RIPGREP_TIMEOUT_S:.0f}s. "
            "Narrow the pattern or path."
        )

    if proc.returncode == 2:
        # rg: 2 = usage/regex error, 1 = no matches, 0 = matches.
        detail = (proc.stderr or proc.stdout).strip()
        return f"Error: invalid search pattern (regex parse error):\n{detail[:500]}"
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or "").strip()
        return f"Error: search failed (exit {proc.returncode}): {detail[:500]}"

    events: list[tuple[str, int, str, bool]] = []  # rel, line, text, is_match
    for raw in proc.stdout.splitlines():
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in ("match", "context"):
            continue
        data = event["data"]
        rel = data["path"]["text"]
        # rg is invoked with the search root as "." and cwd=root, so its
        # paths are already relative to the root (prefix "./" stripped).
        if rel.startswith("./"):
            rel = rel[2:]
        line_no = int(data.get("line_number", 0))
        text = data.get("lines", {}).get("text", "")
        events.append((rel, line_no, text, event.get("type") == "match"))

    return _render_matches(pattern, events, max_results)


# ─────────────────────────── grep: Python fallback ───────────────────────────


def _glob_match(rel: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern.

    Reuses the gitwildmatch converter, which is the same dialect
    ``rg --files -g`` speaks: ``*`` never crosses ``/``, ``**`` spans
    segments, a pattern without ``/`` matches a basename at any depth,
    ``sub/**`` matches the subtree. Keeping one implementation means the
    Python fallback and the ripgrep backend agree on which files a glob
    selects.
    """
    regex, _ = _glob_to_regex(pattern)
    return regex.match(rel) is not None


def _py_grep(
    root: Path,
    pattern: str,
    *,
    glob: str | None,
    case_insensitive: bool,
    max_results: int,
    context: int,
) -> str:
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"Error: invalid search pattern (regex): {exc}"

    levels = _gitignore_levels(root)

    # Collect candidate files first (pruned, sorted) so the scan order is
    # deterministic — and identical to the ripgrep backend's sorted
    # rendering, whatever the filesystem's directory order is.
    candidates: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in NOISE_DIRS
            and not d.startswith(".")
            and not _is_ignored(
                Path(dirpath, d).relative_to(root).as_posix(), levels, is_dir=True
            )
        )
        for filename in sorted(filenames):
            # rg (and us) skip dotfiles by default — no --hidden.
            if filename.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root).replace(
                os.sep, "/"
            )
            if _is_ignored(rel, levels):
                continue
            if glob and not _glob_match(rel, glob):
                continue
            candidates.append((rel, Path(dirpath, filename)))
    candidates.sort(key=lambda pair: pair[0])

    events: list[tuple[str, int, str, bool]] = []
    files_scanned = 0
    for rel, full in candidates:
        if len(events) >= max_results + _CONTEXT_SLACK:
            break
        try:
            if full.is_symlink() or full.stat().st_size > _MAX_FILE_BYTES:
                continue
            raw = full.read_bytes()
        except OSError:
            continue
        files_scanned += 1
        if files_scanned > _MAX_FILES_SCANNED:
            return (
                f"[stopped after scanning {_MAX_FILES_SCANNED} files — "
                "narrow the path or glob]"
            )
        # Binary guard (mirrors ripgrep's default): a NUL byte in the
        # first chunk means the file is not line-oriented text.
        if b"\x00" in raw[:8192]:
            continue
        # rg searches raw bytes, so non-UTF-8 text files can still match;
        # decode with a lossless single-byte fallback to agree.
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1")
        if "\n" not in content and "\r" not in content:
            continue  # not line-oriented text
        numbered = list(enumerate(content.splitlines(), start=1))
        if context > 0:
            matches = [i for i, (_, line) in enumerate(numbered) if regex.search(line)]
            if not matches:
                continue
            seen: set[int] = set()
            for pos in matches:
                for i in range(
                    max(0, pos - context), min(len(numbered), pos + context + 1)
                ):
                    if i in seen:
                        continue
                    seen.add(i)
                    events.append((rel, numbered[i][0], numbered[i][1], i == pos))
        else:
            for line_no, text in numbered:
                if regex.search(text):
                    events.append((rel, line_no, text, True))
    return _render_matches(pattern, events, max_results)


def _grep_single_file(
    root: Path,
    pattern: str,
    *,
    case_insensitive: bool,
    max_results: int,
    context: int,
) -> str:
    """grep restricted to one file (no ignore/glob semantics)."""
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"Error: invalid search pattern (regex): {exc}"
    try:
        content = root.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"Cannot read {root.name} as text (binary or unreadable)."
    numbered = list(enumerate(content.splitlines(), start=1))
    lines: list[str] = []
    count = 0
    if context > 0:
        matches = [i for i, (_, line) in enumerate(numbered) if regex.search(line)]
        seen: set[int] = set()
        ordered: list[tuple[int, str, bool]] = []
        for pos in matches:
            for i in range(
                max(0, pos - context), min(len(numbered), pos + context + 1)
            ):
                if i in seen:
                    continue
                seen.add(i)
                ordered.append((numbered[i][0], numbered[i][1], i == pos))
        for line_no, text, is_match in ordered:
            lines.append(_render_line(root.name, line_no, text))
            if is_match:
                count += 1
                if count >= max_results:
                    lines.append(
                        f"[stopped at {max_results} matching lines — narrow the search]"
                    )
                    break
    else:
        for line_no, text in numbered:
            if regex.search(text):
                lines.append(_render_line(root.name, line_no, text))
                count += 1
                if count >= max_results:
                    lines.append(
                        f"[stopped at {max_results} matching lines — narrow the search]"
                    )
                    break
    if not lines:
        return f"No matches found for pattern {pattern!r}."
    return _render_capped(lines)


def _grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_insensitive: bool = False,
    max_results: int = GREP_DEFAULT_MAX_RESULTS,
    context: int = 0,
) -> str:
    root = _resolve_root(path)
    if not root.exists():
        return f"Path not found: {path}"
    if root.is_file():
        return _grep_single_file(
            root,
            pattern,
            case_insensitive=case_insensitive,
            max_results=max_results,
            context=context,
        )

    if not _rg_available():
        return _py_grep(
            root,
            pattern,
            glob=glob,
            case_insensitive=case_insensitive,
            max_results=max_results,
            context=context,
        )
    return _rg_grep(
        root,
        pattern,
        glob=glob,
        case_insensitive=case_insensitive,
        max_results=max_results,
        context=context,
    )


# ─────────────────────────── glob ───────────────────────────


def _rg_glob(root: Path, pattern: str) -> str:
    cmd = ["rg", "--files", "-g", pattern, "--no-require-git", "."]
    for noise in sorted(NOISE_DIRS):
        cmd += ["--glob", f"!{noise}/"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_RIPGREP_TIMEOUT_S,
            check=False,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        return f"Glob timed out after {_RIPGREP_TIMEOUT_S:.0f}s. Narrow the pattern."
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or "").strip()
        return f"Error: invalid glob pattern: {detail[:500]}"

    entries: list[tuple[float, str]] = []
    # rg --files -g lists gitignored files when an explicit glob matches
    # them (unlike content search, which skips them) — so the same
    # .gitignore rules the fallback applies are re-checked here to keep
    # the two backends, and the documented contract, in lockstep.
    levels = _gitignore_levels(root)
    for raw in proc.stdout.splitlines():
        rel = raw.strip()
        if not rel:
            continue
        # rg is invoked with the search root as "." and cwd=root, so its
        # paths are already relative to the root (prefix "./" stripped).
        if rel.startswith("./"):
            rel = rel[2:]
        if _is_ignored(rel, levels):
            continue
        try:
            mtime = (root / rel).stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append((mtime, rel))

    if not entries:
        return f"No files matched pattern {pattern!r}."
    # Newest first; path as the tie-breaker so the order is deterministic
    # and identical to the Python backend's.
    entries.sort(key=lambda pair: (-pair[0], pair[1]))
    lines = [rel for _, rel in entries[:_GLOB_MAX_RESULTS]]
    if len(entries) > _GLOB_MAX_RESULTS:
        lines.append(
            f"[… {len(entries) - _GLOB_MAX_RESULTS} more — narrow the pattern]"
        )
    return _render_capped(lines)


def _py_glob(root: Path, pattern: str) -> str:
    if not pattern:
        return "Error: invalid glob pattern: empty pattern."

    levels = _gitignore_levels(root)
    entries: list[tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in NOISE_DIRS
            and not _is_ignored(
                Path(dirpath, d).relative_to(root).as_posix(), levels, is_dir=True
            )
        )
        for filename in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, filename), root).replace(
                os.sep, "/"
            )
            if _is_ignored(rel, levels):
                continue
            if not _glob_match(rel, pattern):
                continue
            full = Path(dirpath, filename)
            try:
                mtime = full.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((mtime, rel))

    if not entries:
        return f"No files matched pattern {pattern!r}."
    # Newest first; path as the tie-breaker (same order as the ripgrep
    # backend).
    entries.sort(key=lambda pair: (-pair[0], pair[1]))
    lines = [rel for _, rel in entries[:_GLOB_MAX_RESULTS]]
    if len(entries) > _GLOB_MAX_RESULTS:
        lines.append(
            f"[… {len(entries) - _GLOB_MAX_RESULTS} more — narrow the pattern]"
        )
    return _render_capped(lines)


def _glob(pattern: str, path: str = ".") -> str:
    root = _resolve_root(path)
    if not root.exists():
        return f"Path not found: {path}"
    if root.is_file():
        return f"Not a directory: {path}"
    if _rg_available():
        return _rg_glob(root, pattern)
    return _py_glob(root, pattern)


# ─────────────────────────── tool wrappers ───────────────────────────


@tool
def grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_insensitive: bool = False,
    max_results: int = GREP_DEFAULT_MAX_RESULTS,
    context: int = 0,
) -> str:
    """Search file contents for a regular expression (native, ripgrep-backed).

    Use to find where something is defined, used, or mentioned across the
    repo — prefer this over `bash` with `grep -rn` or `rg`: it is faster,
    respects .gitignore, and returns clean `path:line: content` output
    without shell-quoting pitfalls. `pattern` is a regular expression
    (e.g. `def build_.*\\(`, `TODO|FIXME`). `glob` optionally filters
    which files are searched (e.g. `*.py`, `src/**`); `path` is the
    directory (or single file) to search, relative to the working
    directory. `context` adds N surrounding lines per match (like
    `grep -C`). Results stop at `max_results` matching lines (default
    100, max 1000) — when the output says it stopped, narrow the pattern
    or path. Skips .git, node_modules, __pycache__, .venv, dist, build
    and anything gitignored.
    """
    try:
        max_results = max(1, min(int(max_results), GREP_MAX_RESULTS_HARD))
        context = max(0, min(int(context), 20))
    except (TypeError, ValueError):
        max_results, context = GREP_DEFAULT_MAX_RESULTS, 0
    return _grep(
        pattern,
        path=path,
        glob=glob,
        case_insensitive=case_insensitive,
        max_results=max_results,
        context=context,
    )


@tool
def glob(pattern: str, path: str = ".") -> str:
    """Find files by name pattern (native, .gitignore-aware).

    Use to locate files when you know the name/shape but not the exact
    path (e.g. `tests/**/*.py`, `*.toml`, `**/config*.json`) — prefer
    this over `bash` with `find`. `pattern` is a glob where `*` matches
    within one path segment, `**` spans segments, and a pattern without
    `/` matches a basename at any depth. `path` is the directory to
    search, relative to the working directory. Returns matching paths
    (relative), most recently modified first, capped at 500 results.
    Skips .git, node_modules, __pycache__, .venv, dist, build and
    anything gitignored.
    """
    return _glob(pattern, path=path)
