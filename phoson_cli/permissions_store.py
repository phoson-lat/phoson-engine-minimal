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

from phoson_agent.permissions import (
    LEVEL_ASK,
    LEVEL_DENY,
    LEVEL_ALLOW,
    VALID_LEVELS,
    PermissionPolicy,
    PermissionMiddleware,
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

    return PermissionPolicy(levels=levels, allow_patterns=allow_patterns)


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
    if normalized == LEVEL_ALLOW:
        # Allow is the default for unlisted tools; dropping the entry keeps
        # the file minimal and makes /permissions output unambiguous.
        policy.levels.pop(tool, None)
    else:
        policy.levels[tool] = normalized
    return True


def add_pattern(policy: PermissionPolicy, tool: str, pattern: str) -> None:
    """Add an allow pattern for ``tool`` (deduplicated, order-stable)."""
    patterns = policy.allow_patterns.setdefault(tool, [])
    if pattern not in patterns:
        patterns.append(pattern)


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
) -> PermissionMiddleware:
    """Build the middleware wired to the durable store.

    ``on_ask`` is the interactive callback ``(tool_name, args) -> bool``
    provided by the front end; omitted in non-interactive contexts, where
    ``ask`` fails closed.
    """
    policy = load_policy(path=policy_path)
    return PermissionMiddleware(
        policy=policy,
        on_ask=on_ask,
        match_args=dict(MATCH_ARGS),
    )


__all__ = [
    "DEFAULT_PERMISSIONS_FILE",
    "LEVEL_ASK",
    "LEVEL_ALLOW",
    "LEVEL_DENY",
    "MATCH_ARGS",
    "add_pattern",
    "build_permission_middleware",
    "glob_quote",
    "load_policy",
    "remove_pattern",
    "save_policy",
    "set_level",
]
