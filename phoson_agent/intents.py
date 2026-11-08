"""Intent taxonomy for the permission gate (issue #227, phase 1).

Phase 1 of #144/#227 replaces "allow/deny by tool name" with a small set of
*intent* categories derived from **the tool and its parsed arguments** — not
from the tool's name alone. A policy can then be written against what a call
*does* (read the filesystem, delete files, reach the network, run a program)
instead of against an ever-growing list of command names, which is the only
way to block ``bash("rm -rf /")`` while allowing ``bash("ls")`` without
enumerating either command.

The six categories are intentionally coarse and *effect-based*:

``filesystem_read``   — observe files/directories without mutating them
``filesystem_write``  — create/modify files (including redirects and edits)
``filesystem_delete`` — remove files/directories
``network_outbound``  — initiate a connection to a remote host
``process_spawn``     — run a program that is not otherwise classified
``lang_exec``         — run an interpreter/evaluator (arbitrary code)

Design rules
------------

* **Conservative by construction.** An unrecognised program is
  ``process_spawn`` (the catch-all), never silently read-only. A shell
  construct we cannot fully parse (command substitution, subshells) adds
  ``process_spawn`` so the gate can still ask/deny.
* **A call may map to several intents** (``mv`` writes *and* removes; a
  compound line runs several programs). The permission policy resolves the
  *strictest* configured level across them (see
  :meth:`phoson_agent.permissions.PermissionPolicy.evaluate`).
* **Only known built-in tools are mapped.** An unknown tool — an MCP tool, a
  plugin tool — yields no intents, so the tool-name level and the MCP
  annotation hints (phase 2) keep governing it. The taxonomy never *loosens*
  an existing gate.

This module is pure and dependency-free so it can be reused by any front end
(including Phoson-Core), exactly like the rest of the permission layer.
"""

import os
import re
from typing import Any
from pathlib import Path
from collections.abc import Iterable

# ── Intent vocabulary ────────────────────────────────────────────────────────

INTENT_FILESYSTEM_READ = "filesystem_read"
INTENT_FILESYSTEM_WRITE = "filesystem_write"
INTENT_FILESYSTEM_DELETE = "filesystem_delete"
INTENT_NETWORK_OUTBOUND = "network_outbound"
INTENT_PROCESS_SPAWN = "process_spawn"
INTENT_LANG_EXEC = "lang_exec"
#: Protective, path-derived intents. They are added *on top of* the plain
#: read/write intent when a file tool targets a path outside the workspace
#: (process cwd, resolved through symlinks) or a credential store. Unlike the
#: other intents they carry a built-in default level
#: (:data:`DEFAULT_INTENT_LEVELS`), so the gate asks even with an empty policy.
INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE = "filesystem_write_outside_workspace"
INTENT_FILESYSTEM_READ_SENSITIVE = "filesystem_read_sensitive"

#: Every valid intent, in a stable display order.
VALID_INTENTS: tuple[str, ...] = (
    INTENT_FILESYSTEM_READ,
    INTENT_FILESYSTEM_WRITE,
    INTENT_FILESYSTEM_DELETE,
    INTENT_NETWORK_OUTBOUND,
    INTENT_PROCESS_SPAWN,
    INTENT_LANG_EXEC,
    INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE,
    INTENT_FILESYSTEM_READ_SENSITIVE,
)

#: Levels applied to an intent when the policy does not configure it. Only
#: the protective path intents have one; set the intent explicitly in
#: ``intent_levels`` (e.g. to ``"allow"``) to override.
DEFAULT_INTENT_LEVELS: dict[str, str] = {
    INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE: "ask",
    INTENT_FILESYSTEM_READ_SENSITIVE: "ask",
}

#: Recommended *soft* defaults for hosts that want a safe out-of-the-box gate
#: (the CLI uses them). Unlike :data:`DEFAULT_INTENT_LEVELS` they are consulted
#: only when nothing else decides — no explicit tool level, no intent rule and
#: no auto mode (``"*"``) — so auto mode and any rule the user writes override
#: them. Running programs or code and deleting files ask; reading, editing in
#: the workspace and the web tools stay allowed.
SAFE_DEFAULT_INTENT_LEVELS: dict[str, str] = {
    INTENT_PROCESS_SPAWN: "ask",
    INTENT_LANG_EXEC: "ask",
    INTENT_FILESYSTEM_DELETE: "ask",
}

_VALID_INTENTS = frozenset(VALID_INTENTS)


# ── Built-in tool → intent map (#227) ────────────────────────────────────────
#
# Only tools whose effect is unambiguous are listed. ``bash`` is derived from
# its command line (see :func:`infer_intents`); everything else — MCP and
# plugin tools, sub-agents, monitors — has no intent and keeps its tool-name
# level / annotation hints.

_TOOL_INTENTS: dict[str, frozenset[str]] = {
    "read_file": frozenset({INTENT_FILESYSTEM_READ}),
    "list_dir": frozenset({INTENT_FILESYSTEM_READ}),
    "grep": frozenset({INTENT_FILESYSTEM_READ}),
    "glob": frozenset({INTENT_FILESYSTEM_READ}),
    "view_image": frozenset({INTENT_FILESYSTEM_READ}),
    "write_file": frozenset({INTENT_FILESYSTEM_WRITE}),
    "patch_file": frozenset({INTENT_FILESYSTEM_WRITE}),
    "web_fetch": frozenset({INTENT_NETWORK_OUTBOUND}),
    "web_search": frozenset({INTENT_NETWORK_OUTBOUND}),
}


# ── Program → intent map ─────────────────────────────────────────────────────
#
# Curated and deliberately conservative: a program that is absent is treated
# as ``process_spawn``. Paths are stripped (``/bin/rm`` → ``rm``) before the
# lookup.

_READ_PROGRAMS: frozenset[str] = frozenset(
    {
        "cat",
        "tac",
        "head",
        "tail",
        "less",
        "more",
        "bat",
        "pager",
        "ls",
        "ll",
        "dir",
        "vdir",
        "tree",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "find",
        "fd",
        "fdfind",
        "locate",
        "plocate",
        "which",
        "whereis",
        "type",
        "file",
        "stat",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "diff",
        "colordiff",
        "cmp",
        "comm",
        "join",
        "paste",
        "fold",
        "nl",
        "od",
        "xxd",
        "hexdump",
        "strings",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "cksum",
        "du",
        "df",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "jq",
        "yq",
        "sed",
        "awk",
        "gawk",
        "mawk",  # in-place handled below
    }
)

#: Shell builtins and inspection commands with no side effects. Kept apart
#: from ``_READ_PROGRAMS`` (which read *files*): these read nothing a policy
#: cares about, so they map to ``filesystem_read`` only to stay non-empty.
_INERT_PROGRAMS: frozenset[str] = frozenset(
    {
        "echo",
        "printf",
        "pwd",
        "true",
        "false",
        "test",
        "[",
        "date",
        "whoami",
        "id",
        "uname",
        "hostname",
        "seq",
        "sleep",
        "cd",
        "clear",
    }
)

#: ``git`` subcommands that only read the repository.
_GIT_READ: frozenset[str] = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "shortlog",
        "describe",
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-tree",
        "cat-file",
        "grep",
        "reflog",
        "whatchanged",
        "name-rev",
        "merge-base",
        "count-objects",
        "help",
        "version",
    }
)
#: ``git`` subcommands that reach a remote.
_GIT_NETWORK: frozenset[str] = frozenset(
    {"push", "pull", "fetch", "clone", "ls-remote", "submodule", "remote"}
)
#: ``git`` subcommands that discard work.
_GIT_DELETE: frozenset[str] = frozenset({"clean", "rm"})

#: Programs that print the environment (API keys live there).
_ENV_DUMP_PROGRAMS: frozenset[str] = frozenset({"env", "printenv", "export", "set"})

_WRITE_PROGRAMS: frozenset[str] = frozenset(
    {
        "cp",
        "mv",
        "mkdir",
        "touch",
        "ln",
        "install",
        "chmod",
        "chown",
        "chgrp",
        "tee",
        "dd",
        "truncate",
        "mktemp",
        "mkfifo",
        "mknod",
        "rename",
        "patch",
    }
)

_DELETE_PROGRAMS: frozenset[str] = frozenset({"rm", "rmdir", "unlink", "shred"})

_NETWORK_PROGRAMS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "http",
        "httpie",
        "aria2c",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "lftp",
        "rsync",
        "ping",
        "ping6",
        "traceroute",
        "tracepath",
        "dig",
        "nslookup",
        "host",
        "whois",
        "mtr",
    }
)

#: Package managers and toolchains that reach the network and spawn work.
_PKG_PROGRAMS: frozenset[str] = frozenset(
    {
        "pip",
        "pip3",
        "uv",
        "poetry",
        "pipenv",
        "conda",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "bun",
        "cargo",
        "go",
        "gem",
        "bundle",
        "apt",
        "apt-get",
        "aptitude",
        "dnf",
        "yum",
        "pacman",
        "zypper",
        "brew",
        "snap",
        "flatpak",
    }
)

_LANG_PROGRAMS: frozenset[str] = frozenset(
    {
        "python",
        "python2",
        "python3",
        "pypy",
        "pypy3",
        "node",
        "nodejs",
        "deno",
        "bun",
        "ruby",
        "irb",
        "perl",
        "php",
        "lua",
        "luajit",
        "Rscript",
        "julia",
        "java",
        "javac",
        "kotlin",
        "scala",
        "groovy",
        "bash",
        "sh",
        "dash",
        "zsh",
        "ksh",
        "fish",
        "csh",
        "tcsh",
        "make",
        "cmake",
        "ninja",
        "gcc",
        "g++",
        "clang",
        "clang++",
        "cc",
        "ld",
        "go",
        "rustc",
        "tsc",
        "eval",
    }
)

#: Wrappers that prefix the *real* program; stripped so the intent of the
#: wrapped command is what counts (``sudo rm -rf /`` → ``rm``).
_WRAPPERS: frozenset[str] = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "nohup",
        "command",
        "time",
        "nice",
        "ionice",
        "setsid",
        "stdbuf",
        "exec",
        "xargs",
        "parallel",
    }
)

#: ``VAR=value`` leading assignments (environment for the command).
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _git_intents(tokens: list[str]) -> frozenset[str]:
    """``git <subcommand>``: read-only, network, delete, or a local write."""
    sub = next((t for t in tokens[1:] if not t.startswith("-")), "")
    if sub in _GIT_READ:
        return frozenset({INTENT_FILESYSTEM_READ})
    if sub == "branch" and not any(
        t in {"-d", "-D", "--delete", "-m", "-M", "--move"} for t in tokens
    ):
        return frozenset({INTENT_FILESYSTEM_READ})
    if sub in _GIT_NETWORK:
        return frozenset({INTENT_NETWORK_OUTBOUND, INTENT_FILESYSTEM_WRITE})
    if sub in _GIT_DELETE:
        return frozenset({INTENT_FILESYSTEM_DELETE})
    # commit, add, checkout, switch, reset, merge, rebase, stash, tag, …
    return frozenset({INTENT_FILESYSTEM_WRITE})


_AWK_EXEC = re.compile(r"\bsystem\s*\(|\|\s*getline|\bgetline\b|\|\s*\"|>\s*\"")
_SED_EXEC = re.compile(r"(?:^|[;{}\s/\d$])[ewW](?:\s|$)|/e\b|/w\s")


def _program_intents(program: str, args: Iterable[str]) -> frozenset[str]:
    """Map one program (and its tokens) to a set of intents."""
    tokens = list(args)
    if program == "git":
        return _git_intents(tokens)
    if program in _INERT_PROGRAMS:
        return frozenset({INTENT_FILESYSTEM_READ})
    if program in _ENV_DUMP_PROGRAMS:
        return frozenset({INTENT_FILESYSTEM_READ_SENSITIVE})
    if program in {"find", "fd", "fdfind"}:
        # ``find`` is read-only *unless* it runs a command, deletes or writes:
        # ``-exec``/``-execdir``/``-ok``/``-okdir`` spawn, ``-delete`` removes,
        # ``-fprint``/``-fprintf``/``-fls`` write a file.
        if any(t in {"-exec", "-execdir", "-ok", "-okdir"} for t in tokens):
            return frozenset({INTENT_PROCESS_SPAWN})
        if "-delete" in tokens:
            return frozenset({INTENT_FILESYSTEM_DELETE})
        if any(t in {"-fprint", "-fprint0", "-fprintf", "-fls"} for t in tokens):
            return frozenset({INTENT_FILESYSTEM_WRITE})
        return frozenset({INTENT_FILESYSTEM_READ})
    if program == "sort" and any(
        t == "-o" or t.startswith("-o") or t.startswith("--output") for t in tokens
    ):
        return frozenset({INTENT_FILESYSTEM_WRITE})
    if program in {"awk", "gawk", "mawk"} and any(
        _AWK_EXEC.search(t) for t in tokens[1:]
    ):
        # awk can run commands (system(), "cmd" | getline, print > "file").
        return frozenset({INTENT_LANG_EXEC})
    if program == "sed" and any(
        _SED_EXEC.search(t) for t in tokens[1:] if not t.startswith("-")
    ):
        # GNU sed ``e`` executes and ``w``/``W`` write a file.
        return frozenset({INTENT_LANG_EXEC, INTENT_FILESYSTEM_WRITE})
    if program in _DELETE_PROGRAMS:
        return frozenset({INTENT_FILESYSTEM_DELETE})
    if program in _WRITE_PROGRAMS:
        return frozenset({INTENT_FILESYSTEM_WRITE})
    if program in _NETWORK_PROGRAMS:
        return frozenset({INTENT_NETWORK_OUTBOUND})
    if program in _PKG_PROGRAMS:
        return frozenset({INTENT_NETWORK_OUTBOUND, INTENT_PROCESS_SPAWN})
    if program in _LANG_PROGRAMS:
        return frozenset({INTENT_LANG_EXEC})
    if program in _READ_PROGRAMS:
        # ``sed -i`` / ``awk -i inplace`` mutate in place: promote to write.
        if program in {"sed", "awk", "gawk", "mawk"} and any(
            a == "-i" or a.startswith("-i") or a == "--in-place" for a in tokens
        ):
            return frozenset({INTENT_FILESYSTEM_WRITE})
        return frozenset({INTENT_FILESYSTEM_READ})
    # Unknown program: the conservative catch-all.
    return frozenset({INTENT_PROCESS_SPAWN})


# ── Shell segmentation ───────────────────────────────────────────────────────

_SEPARATORS = frozenset(";&|\n")
#: Redirection marker tokens emitted by :func:`shell_segments`.
_REDIRECT_OPS = frozenset({">", ">>", "<"})


def split_redirects(tokens: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Separate ``(op, target)`` redirections from a segment's other tokens."""
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _REDIRECT_OPS:
            if idx + 1 < len(tokens):
                redirects.append((tok, tokens[idx + 1]))
            idx += 2
            continue
        words.append(tok)
        idx += 1
    return words, redirects


def _strip_path(program: str) -> str:
    return os.path.basename(program) if "/" in program else program


def shell_segments(command: str) -> tuple[list[list[str]], bool]:
    """Split ``command`` into simple-command token lists.

    Returns ``(segments, opaque)`` where each segment is the token list of one
    simple command (the first token is the program) and ``opaque`` is True when
    a construct we cannot safely attribute to a single program was seen — a
    subshell (``(`` / ``)``), command substitution (``$(`` / backticks). The
    caller treats ``opaque`` conservatively, never as "nothing happened".

    Quoting is respected: separators inside **single quotes** are literal, and
    inside **double quotes** only the separators are literal (substitution
    still executes, so it flips ``opaque``). A backslash escapes the next char
    outside single quotes.
    """
    segments: list[list[str]] = []
    current: list[str] = []
    token: list[str] = []
    in_single = in_double = escape = False
    opaque = False
    i, n = 0, len(command)

    def _end_token() -> None:
        if token:
            current.append("".join(token))
            token.clear()

    def _end_segment() -> None:
        _end_token()
        if current:
            segments.append(list(current))
        current.clear()

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if in_single:
            if ch == "'":
                in_single = False
            else:
                token.append(ch)
        elif escape:
            token.append(ch)
            escape = False
        elif in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            elif ch == "`" or (ch == "$" and nxt == "("):
                opaque = True
                token.append(ch)
            else:
                token.append(ch)
        else:  # unquoted
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "`" or (ch == "$" and nxt == "(") or ch in "()":
                opaque = True
                # A subshell/substitution boundary separates commands.
                if ch in "()":
                    _end_segment()
                else:
                    token.append(ch)
            elif ch in _SEPARATORS:
                _end_segment()
            elif ch.isspace():
                # Plain whitespace separates *tokens* of the same command.
                _end_token()
            elif ch in "<>":
                # Redirection. Kept as a marker token (``>``, ``>>``, ``<``)
                # followed by its target, so callers can see the file access:
                # ``>`` writes, ``<`` reads. A leading fd number (``2>``) is
                # dropped; fd duplication (``2>&1``, ``>&-``) targets no file.
                if token and "".join(token).isdigit():
                    token.clear()
                _end_token()
                op = ch
                if nxt == ch:  # ``>>`` / ``<<``
                    op += ch
                    i += 1
                if i + 1 < n and command[i + 1] == "&":
                    i += 1
                    while i + 1 < n and (
                        command[i + 1].isdigit() or command[i + 1] == "-"
                    ):
                        i += 1
                elif op != "<<":  # a heredoc delimiter is not a file
                    current.append(op)
            else:
                token.append(ch)
        i += 1

    _end_segment()
    return segments, opaque


def _segment_program(tokens: list[str]) -> str | None:
    """Effective program name of a token list (wrappers/assignments skipped)."""
    idx = 0
    # Skip leading VAR=value assignments and wrapper commands.
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _REDIRECT_OPS:
            idx += 2  # the operator and its target
            continue
        if _ASSIGNMENT.match(tok):
            idx += 1
            continue
        base = _strip_path(tok)
        if base in _WRAPPERS:
            idx += 1
            continue
        return base
    return None


def _monitor_command(args: dict[str, Any]) -> Any:
    spec = args.get("spec")
    if args.get("kind") == "command" and isinstance(spec, dict):
        return spec.get("command")
    return None


#: Non-``bash`` tools that run a shell command line, and how to get it. Their
#: calls are classified like ``bash`` with that line (plus ``process_spawn``,
#: since they launch a process that outlives the call), and the gate applies
#: ``bash``'s own level to them when they have none — so they cannot be used
#: to sidestep a ``bash`` rule.
SHELL_COMMAND_TOOLS: dict[str, Any] = {
    "run_bg_job": lambda args: args.get("command"),
    "register_monitor": _monitor_command,
}


def infer_intents(tool_name: str, args: dict[str, Any] | None) -> tuple[str, ...]:
    """Derive the intent categories of one tool call.

    Returns a **sorted tuple** of :data:`VALID_INTENTS` members (deterministic
    for logging/tests). An empty tuple means "no intent could be derived" —
    the caller must fall back to the tool-name / annotation decision and never
    treat it as "safe".
    """
    if not isinstance(args, dict):
        args = {}

    extract = SHELL_COMMAND_TOOLS.get(tool_name)
    if extract is not None:
        command = extract(args)
        if not isinstance(command, str) or not command.strip():
            return ()
        return tuple(
            sorted(
                set(infer_intents("bash", {"command": command}))
                | {INTENT_PROCESS_SPAWN}
            )
        )

    if tool_name == "bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ()
        segments, opaque = shell_segments(command)
        intents: set[str] = set()
        for segment in segments:
            words, redirects = split_redirects(segment)
            program = _segment_program(words)
            if program:
                intents |= _program_intents(program, words)
            elif words and _strip_path(words[0]) in _ENV_DUMP_PROGRAMS:
                # Bare ``env`` (a wrapper with nothing to wrap) prints the
                # environment, API keys included.
                intents.add(INTENT_FILESYSTEM_READ_SENSITIVE)
            for op, target in redirects:
                intents |= _redirect_intents(op, target)
            intents |= _shell_path_intents(words[1:] if program else words)
        if opaque:
            # An unparseable construct can run arbitrary code; keep the gate
            # able to ask/deny rather than silently approving it.
            intents = set(intents) | {INTENT_PROCESS_SPAWN}
        return tuple(sorted(intents))

    intents = set(_TOOL_INTENTS.get(tool_name, frozenset()))
    intents |= _path_intents(tool_name, args)
    return tuple(sorted(intents))


# ── Shell path checks ────────────────────────────────────────────────────────

#: Redirect targets that are not files.
_NON_FILE_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def _looks_like_path(token: str) -> bool:
    return token.startswith(("~", "/", "./", "../", ".")) or "/" in token


def _workspace() -> Path | None:
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def _is_sensitive(resolved: Path) -> bool:
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return False
    return any(_is_within(resolved, home / rel) for rel in SENSITIVE_HOME_PATHS)


def _redirect_intents(op: str, target: str) -> set[str]:
    """Intents of one shell redirection (``>``/``>>`` write, ``<`` read)."""
    if target in _NON_FILE_TARGETS:
        return set()
    # The shell expands ``~`` in a redirect target.
    resolved = _resolve_tool_path(target, expand_user=True)
    if op == "<":
        intents = {INTENT_FILESYSTEM_READ}
        if resolved is not None and _is_sensitive(resolved):
            intents.add(INTENT_FILESYSTEM_READ_SENSITIVE)
        return intents
    intents = {INTENT_FILESYSTEM_WRITE}
    workspace = _workspace()
    if resolved is None or workspace is None or not _is_within(resolved, workspace):
        intents.add(INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE)
    return intents


def _shell_path_intents(tokens: list[str]) -> set[str]:
    """``filesystem_read_sensitive`` when an argument names a credential store.

    ``cat ~/.ssh/id_rsa`` must be gated exactly like ``read_file`` on the same
    path; the shell expands ``~``, so it is expanded here too.
    """
    for token in tokens:
        if token.startswith("-") or not _looks_like_path(token):
            continue
        resolved = _resolve_tool_path(token, expand_user=True)
        if resolved is not None and _is_sensitive(resolved):
            return {INTENT_FILESYSTEM_READ_SENSITIVE}
    return set()


# ── Path-derived protective intents ──────────────────────────────────────────
#
# Each built-in file tool resolves its ``path`` argument its own way; the gate
# must resolve it *identically* or it would judge a different file than the
# one the tool touches. ``read_file``/``write_file``/``patch_file``/
# ``list_dir`` use ``Path(path)`` verbatim (``~`` is a literal directory
# name); ``grep``/``glob``/``view_image`` expand ``~`` first.

_WRITE_PATH_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file"})
_READ_PATH_TOOLS: dict[str, bool] = {
    # tool -> expands "~"
    "read_file": False,
    "list_dir": False,
    "grep": True,
    "glob": True,
    "view_image": True,
}

#: Credential stores under the home directory a read should not touch
#: silently (an injected "print ~/.ssh/id_rsa" is the classic exfiltration).
SENSITIVE_HOME_PATHS: tuple[str, ...] = (
    ".phoson",
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".config/gh",
    ".config/gcloud",
    ".docker/config.json",
    ".kube/config",
)


def _resolve_tool_path(raw: str, *, expand_user: bool) -> Path | None:
    try:
        path = Path(raw).expanduser() if expand_user else Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _path_intents(tool_name: str, args: dict[str, Any]) -> set[str]:
    """Protective intents for a file tool's resolved ``path`` argument."""
    if tool_name in _WRITE_PATH_TOOLS:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw:
            return set()
        resolved = _resolve_tool_path(raw, expand_user=False)
        try:
            workspace = Path.cwd().resolve()
        except OSError:
            return {INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE}
        if resolved is None or not _is_within(resolved, workspace):
            return {INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE}
        return set()

    expand = _READ_PATH_TOOLS.get(tool_name)
    if expand is None:
        return set()
    raw = args.get("path", ".")
    if not isinstance(raw, str) or not raw:
        return set()
    resolved = _resolve_tool_path(raw, expand_user=expand)
    if resolved is None:
        return set()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return set()
    for rel in SENSITIVE_HOME_PATHS:
        target = home / rel
        # Reading the store itself or anything inside it — or listing/grepping
        # a directory that *contains* it (``grep -r token ~``) — reveals it.
        if _is_within(resolved, target) or (
            tool_name in {"list_dir", "grep", "glob"} and _is_within(target, resolved)
        ):
            return {INTENT_FILESYSTEM_READ_SENSITIVE}
    return set()


__all__ = [
    "DEFAULT_INTENT_LEVELS",
    "INTENT_FILESYSTEM_DELETE",
    "INTENT_FILESYSTEM_READ_SENSITIVE",
    "INTENT_FILESYSTEM_WRITE_OUTSIDE_WORKSPACE",
    "SENSITIVE_HOME_PATHS",
    "SAFE_DEFAULT_INTENT_LEVELS",
    "split_redirects",
    "SHELL_COMMAND_TOOLS",
    "INTENT_FILESYSTEM_READ",
    "INTENT_FILESYSTEM_WRITE",
    "INTENT_LANG_EXEC",
    "INTENT_NETWORK_OUTBOUND",
    "INTENT_PROCESS_SPAWN",
    "VALID_INTENTS",
    "infer_intents",
    "shell_segments",
]
