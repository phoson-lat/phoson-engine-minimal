"""CLI-side permission store and wiring (IMPROVEMENTS.md A1, phase 1).

Bridges the framework-free :class:`phoson_agent.permissions.PermissionMiddleware`
with the CLI's configuration surface:

- Durable policy lives in ``~/.phoson/permissions.json`` (same convention
  as ``models.json``):

  .. code-block:: json

      {
        "levels": {"bash": "ask", "web_search": "deny"},
        "allow_patterns": {"bash": ["git status", "pytest*"]}
      }

  Allow-patterns only apply to tools listed in :data:`MATCH_ARGS` (bash
  matches its command line — and only when it is a single simple command —
  the file tools match their ``path``, the web tools their ``query``/
  ``url``); every other tool resolves purely by its level.

- Runtime changes made through ``/permissions`` (and "[a] always" answers
  from the confirmation flow) are persisted back to the same file, so
  allowlists survive sessions.
- Interactive ``ask`` calls route through the front end's
  :class:`~phoson_cli.ui_protocols.ConfirmationService`; contexts without
  one (one-shot mode) fail closed inside the middleware.
"""

import json
import logging
from pathlib import Path
from collections.abc import Iterable

from phoson_agent.models import AgentTool
from phoson_agent.permissions import (
    LEVEL_ASK,
    LEVEL_DENY,
    LEVEL_ALLOW,
    VALID_LEVELS,
    WILDCARD_TOOL,
    PermissionPolicy,
    PermissionMiddleware,
    collect_tool_hints,
)

_LOGGER = logging.getLogger("phoson_cli.permissions")

#: Default location of the durable policy file.
DEFAULT_PERMISSIONS_FILE = Path("~/.phoson/permissions.json").expanduser()

#: Tool argument matched against allow patterns for each known tool.
#:
#: This mapping is the *only* way an allow-pattern becomes applicable:
#: a tool not listed here has no match text, so no pattern ever matches
#: it (its calls resolve purely by level). The middleware refuses to
#: guess a fallback argument, because argument order in a tool call is
#: under the model's control (#175/F-07).
#:
#: For ``bash`` the command line must additionally be a *single simple
#: command* before any pattern matches (see
#: ``phoson_agent.permissions.pattern_allows``), so ``git *`` approves
#: ``git status`` but never ``git status; rm -rf /``.
MATCH_ARGS: dict[str, str] = {
    "bash": "command",
    "read_file": "path",
    "write_file": "path",
    "patch_file": "path",
    "list_dir": "path",
    "grep": "path",
    "glob": "path",
    "web_search": "query",
    "web_fetch": "url",
}


def _normalize_level(value: object) -> str | None:
    """Return ``value`` when it is a valid level string, else None."""
    if isinstance(value, str) and value in VALID_LEVELS:
        return value
    return None


def load_policy(path: Path | None = None) -> PermissionPolicy:
    """Read the durable policy from disk (empty policy when absent/broken).

    Malformed files are logged and ignored rather than raised: a broken
    permissions file must never lock the user out of their own tools.
    """
    policy_path = path or DEFAULT_PERMISSIONS_FILE
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PermissionPolicy()
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning("Ignoring unreadable permissions file %s: %s", policy_path, exc)
        return PermissionPolicy()

    if not isinstance(raw, dict):
        return PermissionPolicy()

    levels_raw = raw.get("levels", {})
    levels: dict[str, str] = {}
    if isinstance(levels_raw, dict):
        for tool, value in levels_raw.items():
            level = _normalize_level(value)
            if level is not None:
                levels[str(tool)] = level

    patterns_raw = raw.get("allow_patterns", {})
    allow_patterns: dict[str, list[str]] = {}
    if isinstance(patterns_raw, dict):
        for tool, values in patterns_raw.items():
            if isinstance(values, list):
                cleaned = [str(v) for v in values if isinstance(v, str)]
                if cleaned:
                    allow_patterns[str(tool)] = cleaned

    # #227 phase 1: optional intent taxonomy. Absent in pre-#227 files, so
    # an existing permissions.json keeps working unchanged.
    intent_raw = raw.get("intent_levels", {})
    intent_levels: dict[str, str] = {}
    if isinstance(intent_raw, dict):
        for intent, value in intent_raw.items():
            level = _normalize_level(value)
            if level is not None:
                intent_levels[str(intent)] = level

    return PermissionPolicy(
        levels=levels,
        allow_patterns=allow_patterns,
        intent_levels=intent_levels,
    )


def save_policy(
    policy: PermissionPolicy,
    path: Path | None = None,
) -> Path:
    """Persist the policy to disk (parent dirs created, 0600 like config)."""
    policy_path = path or DEFAULT_PERMISSIONS_FILE
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "levels": dict(sorted(policy.levels.items())),
        "allow_patterns": {
            tool: list(patterns)
            for tool, patterns in sorted(policy.allow_patterns.items())
        },
    }
    # Only emit intent_levels when the user actually wrote some: keeps the
    # file a pre-#227 shape for everyone who has not opted in.
    if policy.intent_levels:
        payload["intent_levels"] = dict(sorted(policy.intent_levels.items()))
    policy_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        policy_path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    return policy_path


def set_level(policy: PermissionPolicy, tool: str, level: str) -> bool:
    """Set ``tool``'s level in-place. Returns False for invalid levels."""
    normalized = _normalize_level(level)
    if normalized is None:
        return False
    if normalized == LEVEL_ALLOW and tool != WILDCARD_TOOL:
        # Allow is the default for unlisted tools; dropping the entry keeps
        # the file minimal and makes /permissions output unambiguous. The
        # wildcard is the exception: `"*": "allow"` is the explicit auto
        # mode, so it must be written to the file to have any effect.
        policy.levels.pop(tool, None)
    else:
        policy.levels[tool] = normalized
    return True


def set_auto_mode(policy: PermissionPolicy, enabled: bool) -> None:
    """Turn the global auto mode on/off by writing/removing the wildcard.

    Auto mode (``"*": "allow"``) lets every tool with no rule of its own run
    freely — including annotated plugin tools such as SSH, which otherwise
    resolve to ``ask`` from their risk hints. A per-tool level or intent rule
    still wins, so this never loosens a rule the user wrote for a tool.
    """
    if enabled:
        policy.levels[WILDCARD_TOOL] = LEVEL_ALLOW
    else:
        policy.levels.pop(WILDCARD_TOOL, None)


def is_auto_mode(policy: PermissionPolicy) -> bool:
    """Whether the policy has the global auto-mode wildcard set."""
    return policy.levels.get(WILDCARD_TOOL) == LEVEL_ALLOW


def add_pattern(policy: PermissionPolicy, tool: str, pattern: str) -> None:
    """Add an allow pattern for ``tool`` (deduplicated, order-stable)."""
    patterns = policy.allow_patterns.setdefault(tool, [])
    if pattern not in patterns:
        patterns.append(pattern)


def set_intent_level(policy: PermissionPolicy, intent: str, level: str) -> bool:
    """Set an intent category's level in-place (#227 phase 1).

    Returns False for an unknown intent or an invalid level. ``allow`` drops
    the entry (it is the fallback), mirroring :func:`set_level`.
    """
    from phoson_agent.intents import VALID_INTENTS

    if intent not in VALID_INTENTS:
        return False
    normalized = _normalize_level(level)
    if normalized is None:
        return False
    if normalized == LEVEL_ALLOW:
        policy.intent_levels.pop(intent, None)
    else:
        policy.intent_levels[intent] = normalized
    return True


def glob_quote(text: str) -> str:
    """Quote ``text`` so it matches *literally* as an allow pattern (T-6).

    Allow patterns are fnmatch globs; "always allow this exact command"
    therefore stores the command with its metacharacters turned into
    literal single-character classes (fnmatch has no backslash escape):
    ``*``→``[*]``, ``?``→``[?]``, ``[``→``[[]``, ``]``→``[]]``.
    The quoted pattern matches exactly the original string and nothing
    else.
    """
    _table = str.maketrans({"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"})
    return text.translate(_table)


def remove_pattern(policy: PermissionPolicy, tool: str, pattern: str) -> bool:
    """Remove an allow pattern. Returns False when it was not present."""
    patterns = policy.allow_patterns.get(tool, [])
    if pattern in patterns:
        patterns.remove(pattern)
        if not patterns:
            policy.allow_patterns.pop(tool, None)
        return True
    return False


def build_permission_middleware(
    *,
    policy_path: Path | None = None,
    on_ask=None,
    on_decision=None,
    classifier=None,
    classifier_auto_allow: bool = False,
    classifier_timeout_s: float = 8.0,
) -> PermissionMiddleware:
    """Build the middleware wired to the durable store.

    ``on_ask`` is the interactive callback ``(tool_name, args) -> bool``
    provided by the front end; omitted in non-interactive contexts, where
    ``ask`` fails closed. ``on_decision`` is the optional audit sink called
    once per decision (#227 phase 3). ``classifier`` is the optional LLM
    guardian consulted at ``ask`` (#227 phase 3).
    """
    policy = load_policy(path=policy_path)
    return PermissionMiddleware(
        policy=policy,
        on_ask=on_ask,
        match_args=dict(MATCH_ARGS),
        on_decision=on_decision,
        classifier=classifier,
        classifier_auto_allow=classifier_auto_allow,
        classifier_timeout_s=classifier_timeout_s,
    )


def apply_tool_hints(
    policy: PermissionPolicy,
    tools: Iterable[AgentTool],
) -> None:
    """Refresh ``policy.hints`` from the loaded tool set (#144 phase 2).

    Called by hosts *after* the engine has loaded its plugins (MCP tool
    discovery runs during plugin initialization), so the annotations a tool
    published in its ``metadata`` reach the permission decision. The mapping
    is **replaced**, not merged, so hints from a previous engine build cannot
    linger after the tool set changes.

    Only tools that actually publish hints are entered; built-in tools are
    left untouched and keep the allow-by-default behaviour.
    """
    policy.hints = collect_tool_hints(
        tool for tool in tools if isinstance(tool, AgentTool)
    )


def refresh_policy(
    middleware: PermissionMiddleware,
    tools: Iterable[AgentTool],
    *,
    policy_path: Path | None = None,
) -> PermissionPolicy:
    """Reload the durable policy into a *live* middleware and re-apply hints.

    ``/permissions …`` and the full-screen auto-mode cycle write the file
    directly; without this the middleware keeps enforcing the policy it loaded
    at startup, so the change would not take effect until a restart. The tool
    hints are refreshed too (annotated plugin tools), since ``load_policy``
    returns a policy with an empty hint map.
    """
    policy = load_policy(path=policy_path)
    apply_tool_hints(policy, tools)
    middleware.policy = policy
    return policy


__all__ = [
    "DEFAULT_PERMISSIONS_FILE",
    "LEVEL_ASK",
    "LEVEL_ALLOW",
    "LEVEL_DENY",
    "MATCH_ARGS",
    "add_pattern",
    "apply_tool_hints",
    "build_permission_middleware",
    "glob_quote",
    "is_auto_mode",
    "load_policy",
    "refresh_policy",
    "remove_pattern",
    "save_policy",
    "set_auto_mode",
    "set_intent_level",
    "set_level",
]
