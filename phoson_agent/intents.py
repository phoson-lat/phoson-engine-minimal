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
from collections.abc import Iterable

# ── Intent vocabulary ────────────────────────────────────────────────────────

INTENT_FILESYSTEM_READ = "filesystem_read"
INTENT_FILESYSTEM_WRITE = "filesystem_write"
INTENT_FILESYSTEM_DELETE = "filesystem_delete"
INTENT_NETWORK_OUTBOUND = "network_outbound"
INTENT_PROCESS_SPAWN = "process_spawn"
INTENT_LANG_EXEC = "lang_exec"

#: Every valid intent, in a stable display order.
VALID_INTENTS: tuple[str, ...] = (
    INTENT_FILESYSTEM_READ,
    INTENT_FILESYSTEM_WRITE,
    INTENT_FILESYSTEM_DELETE,
    INTENT_NETWORK_OUTBOUND,
    INTENT_PROCESS_SPAWN,
    INTENT_LANG_EXEC,
)

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


def _program_intents(program: str, args: Iterable[str]) -> frozenset[str]:
    """Map one program (and its tokens) to a set of intents."""
    tokens = list(args)
    if program in {"find", "fd", "fdfind"}:
        # ``find`` is read-only *unless* it runs a command or deletes:
        # ``-exec``/``-execdir``/``-ok``/``-okdir`` spawn, ``-delete`` removes.
        if any(t in {"-exec", "-execdir", "-ok", "-okdir"} for t in tokens):
            return frozenset({INTENT_PROCESS_SPAWN})
        if "-delete" in tokens:
            return frozenset({INTENT_FILESYSTEM_DELETE})
        return frozenset({INTENT_FILESYSTEM_READ})
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
                # Redirection: not a program token, but its effect is a file
                # access. ``>`` implies a write; ``<`` implies a read.
                _end_token()
                # Consume a doubled ``>>`` as one operator.
                if nxt == ch:
                    i += 1
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
        if _ASSIGNMENT.match(tok):
            idx += 1
            continue
        base = _strip_path(tok)
        if base in _WRAPPERS:
            idx += 1
            continue
        return base
    return None


def infer_intents(tool_name: str, args: dict[str, Any] | None) -> tuple[str, ...]:
    """Derive the intent categories of one tool call.

    Returns a **sorted tuple** of :data:`VALID_INTENTS` members (deterministic
    for logging/tests). An empty tuple means "no intent could be derived" —
    the caller must fall back to the tool-name / annotation decision and never
    treat it as "safe".
    """
    if not isinstance(args, dict):
        args = {}

    if tool_name == "bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ()
        segments, opaque = shell_segments(command)
        intents: set[str] = set()
        for segment in segments:
            program = _segment_program(segment)
            if program:
                intents |= _program_intents(program, segment)
        if opaque:
            # An unparseable construct can run arbitrary code; keep the gate
            # able to ask/deny rather than silently approving it.
            intents = set(intents) | {INTENT_PROCESS_SPAWN}
        return tuple(sorted(intents))

    return tuple(sorted(_TOOL_INTENTS.get(tool_name, frozenset())))


__all__ = [
    "INTENT_FILESYSTEM_DELETE",
    "INTENT_FILESYSTEM_READ",
    "INTENT_FILESYSTEM_WRITE",
    "INTENT_LANG_EXEC",
    "INTENT_NETWORK_OUTBOUND",
    "INTENT_PROCESS_SPAWN",
    "VALID_INTENTS",
    "infer_intents",
    "shell_segments",
]
