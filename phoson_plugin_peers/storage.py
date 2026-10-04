"""On-disk presence registry and mailboxes for named Phoson agents.

Layout under ``<data_dir>/<team>/`` (default ``~/.phoson/peers/default/``)::

    agents/<name>.json          presence: pid, instance, cwd, state, heartbeat
    inbox/<name>/tmp/           in-flight writes (never read)
    inbox/<name>/new/           delivered, not yet read by the recipient
    inbox/<name>/cur/           read (moving new -> cur is the read receipt)

Design notes:

- **One file per message** (maildir style). A sender writes the message into
  the recipient's ``tmp/`` and publishes it with an atomic ``os.replace`` into
  ``new/``, so a reader never sees a half-written message and no cross-process
  lock is needed (works on Linux, macOS and Windows).
- **Disk is the source of truth.** Every process (one per CLI window) only
  reads/writes these files; there is no daemon.
- **Presence is advisory.** A name is *live* while its owner refreshes the
  heartbeat; a crashed process leaves a stale file that the next claimant
  takes over. ``instance`` (a per-plugin-instance token) prevents an engine
  rebuild in the same process from deleting the presence its successor owns.

All APIs are synchronous and cheap (small directories); callers hop to a
worker thread when they care about blocking the event loop.
"""

import os
import re
import json
import time
import uuid
import socket
import logging
from typing import Any
from pathlib import Path
from dataclasses import field, asdict, dataclass

logger = logging.getLogger(__name__)

#: Agent / team names: short, filesystem- and mention-friendly.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: A presence older than this (seconds since the last heartbeat) is stale.
DEFAULT_STALE_SECONDS = 30.0

#: Read messages kept per inbox (``cur/``) before the oldest are pruned.
_MAX_READ_MESSAGES = 500

KIND_REQUEST = "request"  # expects a reply (peer_ask)
KIND_MESSAGE = "message"  # fire-and-forget (peer_send, /tell)
KIND_REPLY = "reply"  # answer to a request (reply_to = request id)
MESSAGE_KINDS = (KIND_REQUEST, KIND_MESSAGE, KIND_REPLY)

STATE_IDLE = "idle"
STATE_BUSY = "busy"
STATE_WAITING = "waiting"


class PeerError(Exception):
    """User-actionable peers error (bad name, name taken, offline peer...)."""


def normalize_name(value: str, what: str = "agent name") -> str:
    """Validate and lowercase an agent/team name, or raise :class:`PeerError`."""
    name = (value or "").strip().lower()
    if not _NAME_RE.match(name):
        raise PeerError(
            f"invalid {what} {value!r}: use 1-64 chars of a-z, 0-9, '.', '_' "
            "or '-', starting with a letter or digit"
        )
    return name


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe for a local pid.

    On Windows ``os.kill(pid, 0)`` is not a probe (it would terminate the
    process), so liveness relies on the heartbeat alone there.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class PeerPresence:
    """One agent's presence record (``agents/<name>.json``)."""

    name: str
    pid: int
    instance: str
    host: str = ""
    cwd: str = ""
    state: str = STATE_IDLE
    waiting_on: str = ""
    started_at: float = field(default_factory=time.time)
    heartbeat: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PeerPresence | None":
        try:
            return cls(
                name=str(raw["name"]),
                pid=int(raw.get("pid", 0)),
                instance=str(raw.get("instance", "")),
                host=str(raw.get("host", "")),
                cwd=str(raw.get("cwd", "")),
                state=str(raw.get("state", STATE_IDLE)),
                waiting_on=str(raw.get("waiting_on", "")),
                started_at=float(raw.get("started_at", 0.0)),
                heartbeat=float(raw.get("heartbeat", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_live(self, stale_seconds: float = DEFAULT_STALE_SECONDS) -> bool:
        """Heartbeat fresh and (same host) pid still running."""
        if time.time() - self.heartbeat > stale_seconds:
            return False
        if self.host and self.host != socket.gethostname():
            return True  # shared FS from another host: trust the heartbeat
        return _pid_alive(self.pid)


@dataclass
class PeerMessage:
    """One message between named agents (one JSON file on disk)."""

    id: str
    sender: str
    recipient: str
    content: str
    kind: str = KIND_MESSAGE
    reply_to: str = ""
    hops: int = 0
    created_at: float = field(default_factory=time.time)
    #: Set on replies to signal the request could not be answered.
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PeerMessage | None":
        try:
            kind = str(raw.get("kind", KIND_MESSAGE))
            if kind not in MESSAGE_KINDS:
                return None
            return cls(
                id=str(raw["id"]),
                sender=str(raw["sender"]),
                recipient=str(raw["recipient"]),
                content=str(raw.get("content", "")),
                kind=kind,
                reply_to=str(raw.get("reply_to", "")),
                hops=int(raw.get("hops", 0)),
                created_at=float(raw.get("created_at", 0.0)),
                error=str(raw.get("error", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


class PeerStore:
    """Filesystem view of one team: presence registry + per-agent mailboxes."""

    def __init__(
        self,
        data_dir: str | Path,
        team: str,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.team = normalize_name(team, "team name")
        self.root = Path(data_dir).expanduser() / self.team
        self.stale_seconds = stale_seconds
        self._agents_dir = self.root / "agents"
        self._agents_dir.mkdir(parents=True, exist_ok=True)

    # ── Presence ──────────────────────────────────────────────────────────

    def _presence_path(self, name: str) -> Path:
        return self._agents_dir / f"{name}.json"

    def get(self, name: str) -> PeerPresence | None:
        raw = _read_json(self._presence_path(name))
        return PeerPresence.from_dict(raw) if raw else None

    def live(self, name: str) -> PeerPresence | None:
        presence = self.get(name)
        if presence is None or not presence.is_live(self.stale_seconds):
            return None
        return presence

    def peers(self, *, include_stale: bool = False) -> list[PeerPresence]:
        result: list[PeerPresence] = []
        for path in sorted(self._agents_dir.glob("*.json")):
            raw = _read_json(path)
            presence = PeerPresence.from_dict(raw) if raw else None
            if presence is None:
                continue
            if include_stale or presence.is_live(self.stale_seconds):
                result.append(presence)
        return result

    def claim(self, name: str, *, instance: str, cwd: str = "") -> PeerPresence:
        """Take ``name`` for this process, or raise if a live peer owns it.

        Re-claiming from the same pid (an engine rebuild) always succeeds and
        hands ownership to the new ``instance``.
        """
        name = normalize_name(name)
        current = self.get(name)
        if (
            current is not None
            and current.pid != os.getpid()
            and current.is_live(self.stale_seconds)
        ):
            raise PeerError(
                f"agent name {name!r} is already in use on team {self.team!r} "
                f"(pid {current.pid}, {current.cwd or 'unknown cwd'}); "
                "pick another --name"
            )
        presence = PeerPresence(
            name=name,
            pid=os.getpid(),
            instance=instance,
            host=socket.gethostname(),
            cwd=cwd,
        )
        self.mailbox_dirs(name)  # make sure the inbox exists before announcing
        _atomic_write_json(self._presence_path(name), asdict(presence))
        return presence

    def update(self, presence: PeerPresence) -> bool:
        """Refresh our presence; False when another instance took it over."""
        current = self.get(presence.name)
        if current is not None and current.instance != presence.instance:
            return False
        presence.heartbeat = time.time()
        _atomic_write_json(self._presence_path(presence.name), asdict(presence))
        return True

    def release(self, presence: PeerPresence) -> None:
        """Remove our presence (only if we still own it)."""
        current = self.get(presence.name)
        if current is None or current.instance != presence.instance:
            return
        try:
            self._presence_path(presence.name).unlink()
        except OSError:
            pass

    # ── Mailboxes ─────────────────────────────────────────────────────────

    def mailbox_dirs(self, name: str) -> tuple[Path, Path, Path]:
        base = self.root / "inbox" / name
        dirs = (base / "tmp", base / "new", base / "cur")
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        return dirs

    def deliver(self, message: PeerMessage) -> Path:
        """Atomically publish ``message`` into the recipient's ``new/``."""
        tmp_dir, new_dir, _cur = self.mailbox_dirs(message.recipient)
        filename = f"{time.time_ns():020d}-{message.id}.json"
        tmp = tmp_dir / filename
        tmp.write_text(
            json.dumps(message.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        final = new_dir / filename
        os.replace(tmp, final)
        return final

    def unread(self, name: str) -> list[tuple[Path, PeerMessage]]:
        """Unread messages for ``name`` in arrival order (non-destructive)."""
        _tmp, new_dir, _cur = self.mailbox_dirs(name)
        result: list[tuple[Path, PeerMessage]] = []
        for path in sorted(new_dir.glob("*.json")):
            raw = _read_json(path)
            message = PeerMessage.from_dict(raw) if raw else None
            if message is None:
                logger.warning("Dropping unreadable peer message %s", path)
                self._quarantine(path)
                continue
            result.append((path, message))
        return result

    def mark_read(self, path: Path) -> bool:
        """Move one message ``new/ -> cur/`` (the read receipt).

        Returns False when it was already consumed (another reader won).
        """
        target = path.parent.parent / "cur" / path.name
        try:
            os.replace(path, target)
        except FileNotFoundError:
            return False
        return True

    def is_read(self, recipient: str, message_id: str) -> bool:
        _tmp, _new, cur_dir = self.mailbox_dirs(recipient)
        return any(cur_dir.glob(f"*-{message_id}.json"))

    def prune_read(self, name: str, keep: int = _MAX_READ_MESSAGES) -> None:
        _tmp, _new, cur_dir = self.mailbox_dirs(name)
        files = sorted(cur_dir.glob("*.json"))
        for path in files[: max(0, len(files) - keep)]:
            try:
                path.unlink()
            except OSError:
                pass

    def _quarantine(self, path: Path) -> None:
        try:
            os.replace(path, path.with_suffix(".bad"))
        except OSError:
            pass


def new_message_id() -> str:
    return uuid.uuid4().hex[:16]
