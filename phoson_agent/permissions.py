"""Per-tool permission model (IMPROVEMENTS.md A1, phase 1).

A declarative, middleware-based permission layer for agent tools. The
:class:`PermissionMiddleware` intercepts every tool call through the
standard ``on_before_tool`` hook and decides whether it may run:

- ``allow`` — execute without asking;
- ``ask``   — consult an injected async callback (human in the loop);
- ``deny``  — refuse with an actionable message.

Tool-level levels can be refined with per-tool glob patterns
(``bash.allow_patterns = ["git *", "pytest*"]``): a matching pattern
overrides the tool's default level. This is deliberately *middleware*
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
                unlisted).
        """
        if match_text:
            for pattern in self.allow_patterns.get(tool_name, []):
                if fnmatch.fnmatch(match_text, pattern):
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
        match_args: Optional mapping of tool name → argument name whose
            value is matched against allow patterns (e.g.
            ``{"bash": "command"}``). Tools not listed fall back to their
            first string argument value.
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
        """Extract the string that allow-patterns match against."""
        arg_name = self.match_args.get(call.tool_name)
        if arg_name is not None:
            value = call.args.get(arg_name)
            return value if isinstance(value, str) else None
        for value in call.args.values():
            if isinstance(value, str):
                return value
        return None

    async def on_before_tool(self, call: ToolCallEvent) -> ToolCallEvent | None:
        """Gate the call; raises :class:`ToolBlockedError` on refusal."""
        tool_name = call.tool_name
        match_text = self._match_text(call)

        if match_text:
            for pattern in self._session_allow.get(tool_name, []):
                if fnmatch.fnmatch(match_text, pattern):
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
]
