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

import json
import asyncio
import fnmatch
import hashlib
import logging
import contextvars
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Callable, Iterable, Awaitable

from phoson_llm.schemas import Message, ModelConfig, ToolCallEvent

from .models import AgentTool
from .intents import VALID_INTENTS, infer_intents
from .exceptions import PhosonAgentError
from .middleware import AgentMiddleware
from .intent_guard import (
    GuardClassifier,
    build_guard_context,
    parse_guard_verdict,
    build_guard_messages,
)

_LOGGER = logging.getLogger(__name__)


class ToolBlockedError(PhosonAgentError):
    """Raised by permission middleware to refuse a tool call.

    ``message`` becomes the tool result returned to the model — it should
    be actionable ("ask the user to adjust /permissions") rather than a
    bare refusal.

    ``decision`` (optional) carries the structured :class:`PermissionDecision`
    so a host can log/export the refusal (the tool runner folds it into the
    ``permission_denied`` step payload, which the OTel plugin exports).
    """

    def __init__(self, message: str, *, decision: "PermissionDecision | None" = None):
        super().__init__(message)
        self.decision = decision


#: How a tool may be invoked. ``ask`` requires a confirmation callback.
LEVEL_ALLOW = "allow"
LEVEL_ASK = "ask"
LEVEL_DENY = "deny"
VALID_LEVELS = frozenset({LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY})

#: Strictness lattice: given two levels, the *stricter* one wins. Used to
#: combine the tool-name level and the intent level of one call (#227).
_LEVEL_STRICTNESS: dict[str, int] = {LEVEL_ALLOW: 0, LEVEL_ASK: 1, LEVEL_DENY: 2}


def strictest_level(*levels: str | None) -> str | None:
    """Return the strictest of ``levels`` (ignoring ``None`` / unknown).

    ``None`` when every argument is ``None`` or an unrecognised level, so a
    caller can distinguish "no rule applies" from a real ``allow``.
    """
    known = [level for level in levels if level in _LEVEL_STRICTNESS]
    if not known:
        return None
    return max(known, key=lambda level: _LEVEL_STRICTNESS[level])


#: Which rule produced a decision (recorded in the audit log, #227 phase 3).
SOURCE_ALLOW_PATTERN = "allow_pattern"
SOURCE_SESSION_PATTERN = "session_pattern"
SOURCE_TOOL_LEVEL = "tool_level"
SOURCE_INTENT = "intent"
SOURCE_TOOL_AND_INTENT = "tool_level+intent"
SOURCE_HINT = "hint"
SOURCE_DEFAULT = "default"
SOURCE_MODE = "mode"
SOURCE_CLASSIFIER = "classifier"

#: Pseudo tool name for a *global default* level (``"*"``). Unlike a regular
#: tool, ``allow`` on the wildcard is meaningful rather than a no-op: it is an
#: explicit opt-in that overrides the annotation hints for every tool that has
#: no rule of its own. This is what the CLI's visible **auto** mode writes, so
#: annotated plugin tools (SSH, MCP, ...) run freely in auto mode while a
#: per-tool level or intent rule still wins.
WILDCARD_TOOL = "*"

#: Callback signature: return True to let the call through.
AskCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass(frozen=True)
class PermissionDecision:
    """Structured record of one permission decision (#227 phase 3).

    Every call that reaches the gate produces one of these, whether it is
    allowed, asked or denied — the audit trail the issue asks for ("who asked
    for what, with which intent, which policy applied, what was decided"),
    exportable through an ``on_decision`` sink (e.g. the OTel plugin).

    ``match_text`` is intentionally **not** stored verbatim: a command line
    may carry a secret (a token passed as an argument). ``arg_digest`` is a
    short, stable hash for correlating repeated calls without leaking them.
    """

    tool_name: str
    level: str
    source: str
    allowed: bool
    reason: str
    intents: tuple[str, ...] = ()
    arg_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form for exporters (OTel attributes, a JSONL sink)."""
        return {
            "tool_name": self.tool_name,
            "level": self.level,
            "source": self.source,
            "allowed": self.allowed,
            "reason": self.reason,
            "intents": list(self.intents),
            "arg_digest": self.arg_digest,
        }


#: Callback signature for the audit sink: called once per decision.
DecisionCallback = Callable[[PermissionDecision], None]


def _arg_digest(args: dict[str, Any] | None) -> str:
    """Short stable hash of a call's args, for the audit record only."""
    if not isinstance(args, dict):
        return ""
    payload = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Tool risk hints (issue #144, phase 2) ────────────────────────────────────
#
# Some tools ship their own risk metadata — notably MCP servers, whose
# ``ToolAnnotations`` carry ``readOnlyHint`` / ``destructiveHint`` /
# ``idempotentHint`` / ``openWorldHint``. We consume it as a *signal*, never
# as a contract: it can only ever make the gate *stricter* than the tool-name
# default (never bypass a user rule), and an absent/partial annotation
# degrades to the safe default. This gives the permission decision risk
# vocabulary beyond "tool name", without the intent taxonomy of phase 1.

#: Metadata key under which a tool (the MCP plugin) publishes its hints.
MCP_ANNOTATIONS_KEY = "mcp_annotations"


@dataclass(frozen=True)
class ToolHints:
    """Normalized risk hints derived from a tool's own metadata.

    Defaults are the *conservative* MCP defaults: an unannotated write tool
    is treated as destructive and open-world. Only an explicit, unambiguous
    ``readOnlyHint`` (annotated, read-only, not destructive) lowers the
    derived level to ``allow``; everything else resolves to ``ask`` so a
    human stays in the loop. A user's explicit level in ``permissions.json``
    always wins over these hints (see :meth:`PermissionPolicy.check`).
    """

    annotated: bool = False
    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False
    open_world: bool = True

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "ToolHints | None":
        """Build hints from a tool's ``metadata`` dict, or None if absent.

        Returns ``None`` when the tool carries no annotations at all, so
        tools without hints (all built-ins) keep their current behaviour
        and are never forced into the safe default by this mechanism.
        """
        raw = metadata.get(MCP_ANNOTATIONS_KEY) if isinstance(metadata, dict) else None
        if not isinstance(raw, dict):
            return None
        return cls(
            annotated=bool(raw.get("annotated", False)),
            read_only=bool(raw.get("read_only", False)),
            destructive=bool(raw.get("destructive", True)),
            idempotent=bool(raw.get("idempotent", False)),
            open_world=bool(raw.get("open_world", True)),
        )

    def derived_level(self) -> str:
        """The safe level implied by the hints: allow read-only, else ask."""
        if self.annotated and self.read_only and not self.destructive:
            return LEVEL_ALLOW
        return LEVEL_ASK


def collect_tool_hints(tools: Iterable[AgentTool]) -> dict[str, ToolHints]:
    """Map tool name → :class:`ToolHints` for tools that publish hints.

    Hosts call this once plugins are loaded (MCP tool discovery is
    synchronous but happens during plugin initialization) and merge the
    result into the policy — e.g. in the CLI::

        policy.hints = collect_tool_hints(engine.tools)

    Tools without recognisable metadata are skipped, so built-in tools keep
    their (allow-by-default) behaviour and only annotated tool families are
    subject to the safe default.
    """
    hints: dict[str, ToolHints] = {}
    for tool in tools:
        parsed = ToolHints.from_metadata(getattr(tool, "metadata", None))
        if parsed is not None:
            hints[tool.name] = parsed
    return hints


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
            to restrict. The special key ``"*"`` (:data:`WILDCARD_TOOL`) is a
            *global default* consulted for tools that have no level of their
            own, **before** annotation hints — so ``"*": "allow"`` is the
            explicit "auto" mode that also lets annotated plugin tools run.
        allow_patterns: Mapping of tool name → glob patterns matched
            against the tool's match text (for bash, the command line).
            A match short-circuits to *allow* even under ``ask``/``deny``
            (e.g. safe git subcommands under a deny-by-default bash).
            For bash the command line must be a single simple command for
            a pattern to match (see :func:`pattern_allows`), so a pattern
            cannot bless a chained or substituted shell line.
        hints: Mapping of tool name → :class:`ToolHints` derived from the
            tool's own metadata (MCP annotations, #144 phase 2). Consulted
            *after* an explicit level and *before* the allow-by-default
            fallback, so it can only tighten — never loosen — the gate.
        intent_levels: Mapping of intent category (see
            :mod:`phoson_agent.intents`) → level (#227 phase 1). Consulted
            *together with* the tool's explicit level, resolving to the
            **strictest** of the two, so an intent rule and a tool rule can
            only ever harden each other. A call whose intents are not listed
            (or a tool that yields no intents) is unaffected, which keeps a
            policy written only with ``levels``/``allow_patterns`` working
            unchanged during the migration.
    """

    levels: dict[str, str] = field(default_factory=dict)
    allow_patterns: dict[str, list[str]] = field(default_factory=dict)
    hints: dict[str, ToolHints] = field(default_factory=dict)
    intent_levels: dict[str, str] = field(default_factory=dict)

    def normalized_levels(self) -> dict[str, str]:
        """Levels with invalid entries dropped."""
        return {k: v for k, v in self.levels.items() if v in VALID_LEVELS}

    def normalized_intent_levels(self) -> dict[str, str]:
        """Intent levels with invalid keys/values dropped."""
        return {
            k: v
            for k, v in self.intent_levels.items()
            if k in VALID_INTENTS and v in VALID_LEVELS
        }

    def intent_level(self, intents: tuple[str, ...]) -> str | None:
        """Strictest configured level across ``intents`` (None when unset)."""
        configured = [
            self.intent_levels[intent]
            for intent in intents
            if self.intent_levels.get(intent) in VALID_LEVELS
        ]
        return strictest_level(*configured)

    def evaluate(
        self,
        tool_name: str,
        match_text: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> tuple[str, str, tuple[str, ...]]:
        """Resolve a call to ``(level, source, intents)``.

        Precedence, highest first:

        1. an allow-pattern hit short-circuits to *allow*
           (``source="allow_pattern"``);
        2. the tool's explicit level and the intent-derived level are
           combined with :func:`strictest_level` — neither can loosen the
           other (``source="tool_level"`` / ``"intent"`` / ``"tool_level+intent"``);
        3. the global default level (``levels["*"]``, when set) applies to any
           remaining tool (*before* hints), so an explicit ``"*": "allow"``
           lets annotated plugin tools run — the CLI's auto mode
           (``source="mode"``);
        4. the level derived from :attr:`hints` (MCP annotations): read-only
           → allow, otherwise the safe default (ask) (``source="hint"``);
        5. unlisted tools with no hints default to *allow*, preserving the
           pre-#144 behaviour for built-in tools (``source="default"``).

        The third returned element is the derived intent tuple, for the audit
        record.
        """
        intents = infer_intents(tool_name, args)

        if match_text:
            for pattern in self.allow_patterns.get(tool_name, []):
                if pattern_allows(tool_name, pattern, match_text):
                    return LEVEL_ALLOW, SOURCE_ALLOW_PATTERN, intents

        explicit = self.levels.get(tool_name)
        intent_lvl = self.intent_level(intents)
        combined = strictest_level(explicit, intent_lvl)
        if combined is not None:
            if explicit is not None and intent_lvl is not None:
                source = SOURCE_TOOL_AND_INTENT
            elif intent_lvl is not None:
                source = SOURCE_INTENT
            else:
                source = SOURCE_TOOL_LEVEL
            return combined, source, intents

        # Global default level (the CLI's auto mode). Consulted *before* the
        # annotation hints so that an explicit `"*": "allow"` covers annotated
        # plugin tools (SSH, MCP, ...) too, not just built-ins. A per-tool
        # level or an intent rule above always wins, so auto can never loosen
        # a rule the user wrote for a specific tool.
        wildcard = self.levels.get(WILDCARD_TOOL)
        if wildcard in VALID_LEVELS:
            return wildcard, SOURCE_MODE, intents

        hints = self.hints.get(tool_name)
        if hints is not None:
            return hints.derived_level(), SOURCE_HINT, intents
        return LEVEL_ALLOW, SOURCE_DEFAULT, intents

    def check(
        self,
        tool_name: str,
        match_text: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> str:
        """Resolve the effective decision level for one call.

        Thin wrapper over :meth:`evaluate` that returns only the level. The
        ``args`` argument (#227) is what makes the intent taxonomy derivable;
        callers that omit it get the pre-#227 behaviour (no intent rule).
        """
        return self.evaluate(tool_name, match_text, args)[0]


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
        on_decision: DecisionCallback | None = None,
        classifier: GuardClassifier | None = None,
        classifier_auto_allow: bool = False,
        classifier_timeout_s: float = 8.0,
    ) -> None:
        self.policy = policy
        self.on_ask = on_ask
        self.match_args = match_args or {}
        # Audit sink (#227 phase 3): called once per decision, best-effort.
        self.on_decision = on_decision
        # LLM guardian (#227 phase 3). ``classifier`` None → disabled.
        self.classifier = classifier
        self.classifier_auto_allow = classifier_auto_allow
        self.classifier_timeout_s = classifier_timeout_s
        # Guardian-safe view of the conversation, captured in on_before_llm.
        # A ContextVar keeps parallel sub-agents from clobbering each other.
        self._guard_context: contextvars.ContextVar[list[Message]] = (
            contextvars.ContextVar("phoson_guard_context", default=[])
        )
        # Runtime additions from "[a] always for this pattern" answers.
        # Session-scoped by design: config.toml holds the durable rules.
        self._session_allow: dict[str, list[str]] = {}

    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        """Snapshot the guardian-safe user turns before the model acts.

        Captured here (not at tool time) so the current assistant turn — whose
        only purpose would be to justify the very action being judged — can
        never reach the classifier. Only genuine user turns survive (see
        :func:`~phoson_agent.intent_guard.build_guard_context`).
        """
        if self.classifier is not None:
            self._guard_context.set(build_guard_context(messages))
        return messages

    async def _consult_classifier(self, call: ToolCallEvent) -> Any:
        """Ask the LLM guardian about one call; fail closed to UNSURE.

        Returns a :class:`~phoson_agent.intent_guard.GuardDecision`. Any
        classifier error or timeout degrades to ``UNSURE`` — never ``ALLOW``.
        """
        from .intent_guard import GUARD_UNSURE, GuardDecision

        classifier = self.classifier
        if classifier is None:
            return GuardDecision(GUARD_UNSURE, "classifier disabled")
        messages = build_guard_messages(self._guard_context.get(), call)
        try:
            reply = await asyncio.wait_for(
                classifier(messages), timeout=self.classifier_timeout_s
            )
        except TimeoutError:
            _LOGGER.warning("permission guardian timed out for %s", call.tool_name)
            return GuardDecision(GUARD_UNSURE, "classifier timed out")
        except Exception:  # noqa: BLE001 — a broken guardian must not allow a call
            _LOGGER.warning(
                "permission guardian failed for %s", call.tool_name, exc_info=True
            )
            return GuardDecision(GUARD_UNSURE, "classifier error")
        return parse_guard_verdict(reply)

    def add_session_pattern(self, tool_name: str, pattern: str) -> None:
        """Register an interactive 'always allow <pattern>' grant."""
        patterns = self._session_allow.setdefault(tool_name, [])
        if pattern not in patterns:
            patterns.append(pattern)

    def _emit(self, decision: PermissionDecision) -> None:
        """Hand a decision to the audit sink, never letting it break a run."""
        if self.on_decision is None:
            return
        try:
            self.on_decision(decision)
        except Exception:  # noqa: BLE001 — observability must never block a call
            _LOGGER.warning("permission audit sink failed", exc_info=True)

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
        args = call.args if isinstance(call.args, dict) else {}
        match_text = self._match_text(call)

        if match_text:
            for pattern in self._session_allow.get(tool_name, []):
                if pattern_allows(tool_name, pattern, match_text):
                    self._emit(
                        PermissionDecision(
                            tool_name=tool_name,
                            level=LEVEL_ALLOW,
                            source=SOURCE_SESSION_PATTERN,
                            allowed=True,
                            reason="always-allow grant for this session",
                            arg_digest=_arg_digest(args),
                        )
                    )
                    return call

        level, source, intents = self.policy.evaluate(tool_name, match_text, args)

        if level == LEVEL_ALLOW:
            self._emit(
                PermissionDecision(
                    tool_name=tool_name,
                    level=LEVEL_ALLOW,
                    source=source,
                    allowed=True,
                    reason="allowed by policy",
                    intents=intents,
                    arg_digest=_arg_digest(args),
                )
            )
            return call

        if level == LEVEL_DENY:
            decision = PermissionDecision(
                tool_name=tool_name,
                level=LEVEL_DENY,
                source=source,
                allowed=False,
                reason="denied by permissions policy",
                intents=intents,
                arg_digest=_arg_digest(args),
            )
            self._emit(decision)
            raise ToolBlockedError(
                _denied_message(tool_name, "denied by permissions policy"),
                decision=decision,
            )

        # LLM guardian (#227 phase 3): consulted only here, where the
        # deterministic policy is already unsure. It can deny (and the run
        # continues) or — with auto-allow opted in — skip the human; anything
        # else (UNSURE/error/timeout) falls through to the normal path.
        if self.classifier is not None:
            verdict = await self._consult_classifier(call)
            if verdict.is_deny:
                decision = PermissionDecision(
                    tool_name=tool_name,
                    level=LEVEL_ASK,
                    source=SOURCE_CLASSIFIER,
                    allowed=False,
                    reason=f"guardian denied: {verdict.rationale}",
                    intents=intents,
                    arg_digest=_arg_digest(args),
                )
                self._emit(decision)
                raise ToolBlockedError(
                    _denied_message(tool_name, "denied by the safety guardian"),
                    decision=decision,
                )
            if verdict.is_allow and self.classifier_auto_allow:
                decision = PermissionDecision(
                    tool_name=tool_name,
                    level=LEVEL_ASK,
                    source=SOURCE_CLASSIFIER,
                    allowed=True,
                    reason=f"guardian allowed: {verdict.rationale}",
                    intents=intents,
                    arg_digest=_arg_digest(args),
                )
                self._emit(decision)
                return call

        # ask — human in the loop, or fail closed without a callback.
        if self.on_ask is None:
            decision = PermissionDecision(
                tool_name=tool_name,
                level=LEVEL_ASK,
                source=source,
                allowed=False,
                reason="confirmation required but unavailable here",
                intents=intents,
                arg_digest=_arg_digest(args),
            )
            self._emit(decision)
            raise ToolBlockedError(
                _denied_message(
                    tool_name, "confirmation required but unavailable here"
                ),
                decision=decision,
            )
        granted = await self.on_ask(tool_name, call.args)
        decision = PermissionDecision(
            tool_name=tool_name,
            level=LEVEL_ASK,
            source=source,
            allowed=granted,
            reason="approved by the user" if granted else "denied by the user",
            intents=intents,
            arg_digest=_arg_digest(args),
        )
        self._emit(decision)
        if granted:
            return call
        raise ToolBlockedError(
            _denied_message(tool_name, "denied by the user"), decision=decision
        )


__all__ = [
    "AskCallback",
    "DecisionCallback",
    "MCP_ANNOTATIONS_KEY",
    "PermissionDecision",
    "PermissionMiddleware",
    "PermissionPolicy",
    "SOURCE_ALLOW_PATTERN",
    "SOURCE_CLASSIFIER",
    "SOURCE_DEFAULT",
    "SOURCE_HINT",
    "SOURCE_INTENT",
    "SOURCE_MODE",
    "SOURCE_SESSION_PATTERN",
    "SOURCE_TOOL_AND_INTENT",
    "SOURCE_TOOL_LEVEL",
    "ToolBlockedError",
    "ToolHints",
    "VALID_LEVELS",
    "WILDCARD_TOOL",
    "collect_tool_hints",
    "is_simple_shell_command",
    "pattern_allows",
    "strictest_level",
]
