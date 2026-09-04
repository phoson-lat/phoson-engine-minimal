"""Per-tool permission model (IMPROVEMENTS.md A1, phase 1).

A declarative, middleware-based permission layer for agent tools. The
:class:`PermissionMiddleware` intercepts every tool call through the
standard ``on_before_tool`` hook and decides whether it may run:

- ``allow`` — execute without asking;
- ``ask``   — consult an injected async callback (human in the loop);
- ``deny``  — refuse with an actionable message.

Tool-level levels can be refined with per-tool glob patterns
(``bash.allow_patterns = ["git *", "pytest*"]``): a matching pattern
overrides the tool's default level. For ``bash`` a pattern only authorizes
a *single simple command* — a compound shell line (``;``, ``&``, ``|``,
``$( ``) never matches, so ``git *`` allows ``git status`` but not
``git status; rm -rf /``. Patterns apply only to tools that declare a
``match_args`` entry, so they can't be steered onto an unintended argument.
This is deliberately *middleware*
(not hardcoded into tools) so it stays framework-free and reusable by any
front end — including Phoson-Core.

The ask callback receives ``(tool_name, args)`` and returns True/False.
When no callback is configured (one-shot / non-interactive contexts) an
``ask`` level fails **closed**: the call is refused rather than executed.

Denials raise :class:`ToolBlockedError` carrying an actionable message;
:class:`phoson_agent._tool_runner.ToolRunner` catches it and feeds the
message to the model as the tool result, so the refusal is visible to
both the user and the agent.
"""

import fnmatch
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from phoson_llm.schemas import ToolCallEvent

from .exceptions import PhosonAgentError
from .middleware import AgentMiddleware


class ToolBlockedError(PhosonAgentError):
    """Raised by permission middleware to refuse a tool call.

    ``message`` becomes the tool result returned to the model — it should
    be actionable ("ask the user to adjust /permissions") rather than a
    bare refusal.
    """


#: How a tool may be invoked. ``ask`` requires a confirmation callback.
LEVEL_ALLOW = "allow"
LEVEL_ASK = "ask"
LEVEL_DENY = "deny"
VALID_LEVELS = frozenset({LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY})

#: Callback signature: return True to let the call through.
AskCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


# ── Bash allow-pattern safety (F-03, F-07, #175) ─────────────────────────────
#
# An allow-pattern may only authorize a *single simple command* — one program
# run. The old implementation ran ``fnmatch`` over the whole shell line, so a
# pattern like ``git *`` also matched ``git status; rm -rf /``, ``git log |
# sh`` and ``git $(rm -rf /)``: the pattern blessed the *first* command but the
# shell went on to run the rest. The helpers below make a bash pattern match
# only when the line is a single simple command, so a line that can run more
# than one program (or substitute one in) is never auto-approved by a pattern
# and falls back to the tool's configured level (usually ``ask``/``deny``).

#: Characters that, in a shell-active position, chain or background commands.
_COMPOUND_SEPARATORS = frozenset(";&|\n")
#: Characters that, in a shell-active position, open command substitution or a
#: subshell. ``$(`` is a two-char sequence and is handled separately.
_COMPOUND_CHARS = frozenset("`()")


def is_simple_shell_command(command: str) -> bool:
    """Return True only when ``command`` is a single *simple* shell command.

    A simple command runs exactly one program. Anything that lets the shell
    run *more than one* program — or substitute another command in — makes the
    line **compound** and returns False:

    - separators ``;``, ``&`` (``&&``), ``|`` (``||``), newline;
    - command substitution `` ` ``, ``$( ``;
    - subshell grouping ``(`` ``)``.

    Quoting is respected: operators inside **single quotes** are fully
    literal (``git commit -m 'a; b'`` is a single command). Inside **double
    quotes** the separators are literal, but command substitution (`` ` ``,
    ``$( ``) still executes, so those are still flagged. A backslash escapes
    the following character outside single quotes.

    Deliberately conservative: on any doubt it returns False, so the gate
    falls back to ``ask`` (safe) instead of auto-allowing a compound line.
    """
    in_single = False
    in_double = False
    escape = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if in_single:
            if ch == "'":
                in_single = False
        elif escape:
            escape = False  # this char was escaped by a previous backslash
        elif in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            elif ch == "`" or (ch == "$" and nxt == "("):
                # Substitution is active even inside double quotes.
                return False
        else:  # unquoted
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch in _COMPOUND_SEPARATORS or ch in _COMPOUND_CHARS:
                return False
            elif ch == "$" and nxt == "(":
                return False
        i += 1
    return True


def pattern_allows(tool_name: str, pattern: str, match_text: str) -> bool:
    """Return True when ``pattern`` authorizes ``match_text`` for ``tool_name``.

    For ``bash`` a pattern matches only when ``match_text`` is a single simple
    command (see :func:`is_simple_shell_command`); a compound shell line is
    never auto-allowed by a pattern. Other tools are matched as plain globs.
    """
    if tool_name == "bash" and not is_simple_shell_command(match_text):
        return False
    return fnmatch.fnmatch(match_text, pattern)


@dataclass
class PermissionPolicy:
    """Declarative table of per-tool permission levels and patterns.

    Args:
        levels: Mapping of tool name → default level for that tool.
            Tools not listed are allowed: the engine's tool registry is
            already the curated capability surface, so a policy only needs
            to restrict.
        allow_patterns: Mapping of tool name → glob patterns matched
            against the tool's match text (for bash, the command line).
            A match short-circuits to *allow* even under ``ask``/``deny``
            (e.g. safe git subcommands under a deny-by-default bash).
            For bash the command line must be a single simple command for
            a pattern to match (see :func:`pattern_allows`), so a pattern
            cannot bless a chained or substituted shell line.
    """

    levels: dict[str, str] = field(default_factory=dict)
    allow_patterns: dict[str, list[str]] = field(default_factory=dict)

    def normalized_levels(self) -> dict[str, str]:
        """Levels with invalid entries dropped."""
        return {k: v for k, v in self.levels.items() if v in VALID_LEVELS}

    def check(self, tool_name: str, match_text: str | None = None) -> str:
        """Resolve the effective decision for one call.

        Args:
            tool_name: Name of the tool being called.
            match_text: Optional string matched against the tool's allow
                patterns. A pattern hit short-circuits to *allow*;
                otherwise the tool's configured level applies (allow when
                unlisted). For ``bash``, a pattern only matches a single
                *simple* command (see :func:`pattern_allows`): a compound
                shell line never short-circuits to allow and falls back to
                the configured level.
        """
        if match_text:
            for pattern in self.allow_patterns.get(tool_name, []):
                if pattern_allows(tool_name, pattern, match_text):
                    return LEVEL_ALLOW
        return self.levels.get(tool_name, LEVEL_ALLOW)


def _denied_message(tool_name: str, reason: str) -> str:
    """Actionable refusal text returned to the LLM as the tool result."""
    return (
        f"Blocked: {tool_name} was not executed ({reason}). "
        "Do not retry the same call. Tell the user they can adjust "
        "permissions with /permissions and ask them how to proceed."
    )


class PermissionMiddleware(AgentMiddleware):
    """Enforce a :class:`PermissionPolicy` on every tool call.

    Args:
        policy: The resolved policy table.
        on_ask: Async callback consulted for ``ask``-level calls.
            Receives ``(tool_name, args)`` and returns True to proceed.
            ``None`` means fail closed (non-interactive contexts).
        match_args: Mapping of tool name → argument name whose value is
            matched against allow patterns (e.g. ``{"bash": "command"}``).
            Allow-patterns apply **only** to tools listed here: a tool
            without an explicit entry never matches a pattern (its args are
            not trusted as match text), so a pattern can only ever target
            the argument the caller intends — never a free-floating string
            the model chose to put in another argument.
    """

    def __init__(
        self,
        policy: PermissionPolicy,
        on_ask: AskCallback | None = None,
        match_args: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy
        self.on_ask = on_ask
        self.match_args = match_args or {}
        # Runtime additions from "[a] always for this pattern" answers.
        # Session-scoped by design: config.toml holds the durable rules.
        self._session_allow: dict[str, list[str]] = {}

    def add_session_pattern(self, tool_name: str, pattern: str) -> None:
        """Register an interactive 'always allow <pattern>' grant."""
        patterns = self._session_allow.setdefault(tool_name, [])
        if pattern not in patterns:
            patterns.append(pattern)

    def _match_text(self, call: ToolCallEvent) -> str | None:
        """Extract the string that allow-patterns match against.

        Returns ``None`` — no pattern applies — unless the tool has an
        explicit ``match_args`` entry *and* that argument is a string.
        There is deliberately no "first string argument" fallback: the
        argument order of a tool call is under the model's control, so
        falling back to an unlisted argument would let the model steer a
        pattern toward, e.g., ``content`` instead of ``path``.
        """
        arg_name = self.match_args.get(call.tool_name)
        if arg_name is None:
            return None
        value = call.args.get(arg_name)
        return value if isinstance(value, str) else None

    async def on_before_tool(self, call: ToolCallEvent) -> ToolCallEvent | None:
        """Gate the call; raises :class:`ToolBlockedError` on refusal."""
        tool_name = call.tool_name
        match_text = self._match_text(call)

        if match_text:
            for pattern in self._session_allow.get(tool_name, []):
                if pattern_allows(tool_name, pattern, match_text):
                    return call

        decision = self.policy.check(tool_name, match_text)

        if decision == LEVEL_ALLOW:
            return call

        if decision == LEVEL_DENY:
            raise ToolBlockedError(
                _denied_message(tool_name, "denied by permissions policy")
            )

        # ask — human in the loop, or fail closed without a callback.
        if self.on_ask is None:
            raise ToolBlockedError(
                _denied_message(tool_name, "confirmation required but unavailable here")
            )
        granted = await self.on_ask(tool_name, call.args)
        if granted:
            return call
        raise ToolBlockedError(_denied_message(tool_name, "denied by the user"))


__all__ = [
    "AskCallback",
    "PermissionMiddleware",
    "PermissionPolicy",
    "ToolBlockedError",
    "VALID_LEVELS",
    "is_simple_shell_command",
    "pattern_allows",
]
