"""Browser use through the official Chrome DevTools MCP server (issue #224).

Instead of a browser-automation plugin of its own, phoson-cli configures the
official ``chrome-devtools-mcp`` server (maintained by the Chrome DevTools
team) as an entry of the MCP config, and drives the user's own browser with
it over the Chrome DevTools Protocol:

- **profile** mode (default): the server launches the user's installed
  Chrome — or Edge / Brave / Chromium, passed as ``--executablePath`` — with
  a dedicated profile under ``~/.phoson/browser-profile``. Logins the agent
  makes persist there; the user's own profile and sessions are untouched.
- **chrome** mode: ``--autoConnect`` attaches to the Chrome the user already
  has open (Chrome 144+), with its real profile and sessions. The user must
  enable remote debugging in ``chrome://inspect/#remote-debugging`` first,
  which is the explicit consent step.

Everything the server needs is in the MCP entry's ``command``/``args``; the
user-facing choices (mode, allowed / blocked URL patterns) are also kept
under the entry's ``phoson`` key so ``/browser`` can show and edit them.
Defaults favour privacy and context size: Google usage statistics and CrUX
lookups are off, sensitive network headers are redacted, and only the
navigation / input / debugging / network tool categories are exposed.
"""

import os
import re
import sys
import json
import shutil
import subprocess
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass

#: The MCP server entry name in ``mcps.json``.
SERVER_NAME = "chrome-devtools"
#: Pinned server version (never ``@latest``: no unreviewed code at startup).
SERVER_PACKAGE = "chrome-devtools-mcp"
SERVER_VERSION = "1.12.0"
#: Minimum Node.js major version (an LTS line).
MIN_NODE_MAJOR = 20
#: First start downloads the package with npx; allow for it.
STARTUP_TIMEOUT_S = 120

MODES = ("profile", "chrome")
DEFAULT_MODE = "profile"

#: Tool categories turned off by default (fewer tool schemas per request).
_DISABLED_CATEGORIES = ("Emulation", "Performance", "Memory")

#: Rarely needed tools left off by default (``/mcp toggle`` re-enables them).
DISABLED_TOOLS = (
    "screencast_start",
    "screencast_stop",
    "lighthouse_audit",
    "get_css_styles",
)

#: Tools that only look at the page. The server is conservative and marks a
#: snapshot or a screenshot as not read-only, which would make the agent ask
#: before every look. Corrected through the entry's ``annotations`` (the
#: user's own config). Navigating is "looking" in the isolated profile (like
#: web_fetch) but not in the user's real Chrome, where it acts with their
#: sessions.
_LOOK_TOOLS = (
    "take_snapshot",
    "take_screenshot",
    "wait_for",
    "list_pages",
    "select_page",
    "list_console_messages",
    "get_console_message",
    "list_network_requests",
    "get_network_request",
)
_PROFILE_LOOK_TOOLS = ("navigate_page", "new_page")


@dataclass
class BrowserSettings:
    """The user's browser-use choices (kept under the entry's ``phoson`` key)."""

    mode: str = DEFAULT_MODE
    executable: str | None = None
    allowed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "executable": self.executable,
            "allowed": list(self.allowed),
            "blocked": list(self.blocked),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "BrowserSettings":
        if not isinstance(raw, dict):
            return cls()
        raw_mode = raw.get("mode")
        mode = (
            raw_mode
            if isinstance(raw_mode, str) and raw_mode in MODES
            else DEFAULT_MODE
        )
        executable = raw.get("executable")

        def _patterns(key: str) -> list[str]:
            values = raw.get(key)
            return (
                [str(v) for v in values if isinstance(v, str)]
                if isinstance(values, list)
                else []
            )

        return cls(
            mode=mode,
            executable=executable if isinstance(executable, str) else None,
            allowed=_patterns("allowed"),
            blocked=_patterns("blocked"),
        )


def profile_dir() -> Path:
    """Dedicated browser profile for the agent (profile mode)."""
    home = Path(os.environ.get("PHOSON_HOME", "~/.phoson")).expanduser()
    return home / "browser-profile"


# ── Detection ────────────────────────────────────────────────────────────────


def node_status() -> tuple[bool, str]:
    """``(ok, message)`` for the Node.js/npx the server needs."""
    npx = shutil.which("npx")
    node = shutil.which("node")
    if not npx or not node:
        return False, (
            "Node.js is not installed (the Chrome DevTools MCP server runs on "
            f"it). Install an LTS version (>= {MIN_NODE_MAJOR}) from "
            "https://nodejs.org and run /browser setup again."
        )
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, "Could not run `node --version`."
    match = re.match(r"v(\d+)", out)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        return False, (
            f"Node.js {out or '(unknown)'} is too old; install an LTS version "
            f">= {MIN_NODE_MAJOR} from https://nodejs.org."
        )
    return True, f"Node.js {out}"


def _candidate_browsers() -> list[tuple[str, str]]:
    """``(name, executable)`` candidates for this OS, Chrome first."""
    if sys.platform == "darwin":
        apps = [
            ("Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
        ]
        roots = [Path("/Applications"), Path.home() / "Applications"]
        return [(name, str(root / rel)) for name, rel in apps for root in roots]
    if sys.platform == "win32":
        bases = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rels = [
            ("Chrome", r"Google\Chrome\Application\chrome.exe"),
            ("Edge", r"Microsoft\Edge\Application\msedge.exe"),
            ("Brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            ("Chromium", r"Chromium\Application\chrome.exe"),
        ]
        return [
            (name, str(Path(base) / rel))
            for name, rel in rels
            for base in bases
            if base
        ]
    found: list[tuple[str, str]] = []
    for name, commands in (
        ("Chrome", ("google-chrome", "google-chrome-stable")),
        ("Edge", ("microsoft-edge", "microsoft-edge-stable")),
        ("Brave", ("brave-browser", "brave")),
        ("Chromium", ("chromium", "chromium-browser")),
    ):
        for command in commands:
            path = shutil.which(command)
            if path:
                found.append((name, path))
                break
    return found


def detect_browser() -> tuple[str, str] | None:
    """The first installed Chromium-based browser: ``(name, executable)``."""
    for name, path in _candidate_browsers():
        if Path(path).is_file():
            return name, path
    return None


# ── MCP entry ────────────────────────────────────────────────────────────────


def build_server_entry(settings: BrowserSettings) -> dict[str, Any]:
    """The ``mcps.json`` entry that runs the server for ``settings``."""
    args = [
        "-y",
        f"{SERVER_PACKAGE}@{SERVER_VERSION}",
        "--no-usage-statistics",
        "--performanceCrux=false",
        "--redactNetworkHeaders=true",
        *(f"--category{category}=false" for category in _DISABLED_CATEGORIES),
    ]
    if settings.mode == "chrome":
        args.append("--autoConnect")
    else:
        args.append(f"--userDataDir={profile_dir()}")
        if settings.executable:
            args.append(f"--executablePath={settings.executable}")
    args += [f"--allowedUrlPattern={pattern}" for pattern in settings.allowed]
    args += [f"--blockedUrlPattern={pattern}" for pattern in settings.blocked]
    look = _LOOK_TOOLS + (_PROFILE_LOOK_TOOLS if settings.mode == "profile" else ())
    return {
        "transport": "stdio",
        "command": "npx",
        "args": args,
        "env": {"CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"},
        "startup_timeout": STARTUP_TIMEOUT_S,
        # Clicking, typing, forms, uploads and evaluate_script keep the
        # server's own (asking) classification.
        "annotations": {
            name: {"readOnlyHint": True, "destructiveHint": False} for name in look
        },
        "tools": {name: False for name in DISABLED_TOOLS},
        "phoson": settings.to_dict(),
    }


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    servers = data.get("mcpServers")
    if servers is None:
        data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise ValueError(f"'mcpServers' in {path} is not an object")
    return data


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)  # the file can hold secrets of other servers
    os.replace(tmp, path)


def current_settings(path: Path) -> BrowserSettings | None:
    """The configured browser settings, or ``None`` when not set up."""
    entry = _read_config(path)["mcpServers"].get(SERVER_NAME)
    if not isinstance(entry, dict):
        return None
    return BrowserSettings.from_dict(entry.get("phoson"))


def is_enabled(path: Path) -> bool:
    entry = _read_config(path)["mcpServers"].get(SERVER_NAME)
    return isinstance(entry, dict) and bool(entry.get("enabled", True))


def save_settings(
    path: Path, settings: BrowserSettings, *, enabled: bool = True
) -> None:
    """Write (or rewrite) the server entry, keeping other servers untouched."""
    data = _read_config(path)
    entry = build_server_entry(settings)
    previous = data["mcpServers"].get(SERVER_NAME)
    if isinstance(previous, dict) and isinstance(previous.get("tools"), dict):
        entry["tools"] = {**entry["tools"], **previous["tools"]}  # keep toggles
    entry["enabled"] = enabled
    data["mcpServers"][SERVER_NAME] = entry
    _write_config(path, data)


def remove(path: Path) -> bool:
    data = _read_config(path)
    if SERVER_NAME not in data["mcpServers"]:
        return False
    del data["mcpServers"][SERVER_NAME]
    _write_config(path, data)
    return True


_PATTERN_RE = re.compile(r"^[a-z*][a-z0-9+.*-]*://\S+$", re.I)


def normalize_pattern(value: str) -> str:
    """A URL pattern for the server from a domain or a full pattern.

    ``example.com`` → ``*://example.com/*``; ``*.example.com`` →
    ``*://*.example.com/*``; anything with a scheme is kept as given.
    """
    value = value.strip()
    if not value:
        raise ValueError("empty pattern")
    if _PATTERN_RE.match(value):
        return value
    host = value.strip("/")
    if "/" in host or " " in host:
        raise ValueError(
            f"{value!r} is not a domain; give a domain such as example.com or a "
            "full URL pattern such as https://example.com/*"
        )
    return f"*://{host}/*"


__all__ = [
    "DEFAULT_MODE",
    "MODES",
    "SERVER_NAME",
    "SERVER_VERSION",
    "BrowserSettings",
    "build_server_entry",
    "current_settings",
    "detect_browser",
    "is_enabled",
    "node_status",
    "normalize_pattern",
    "profile_dir",
    "remove",
    "save_settings",
]
