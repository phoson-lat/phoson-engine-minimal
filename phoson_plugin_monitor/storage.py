"""Persistent state for the monitor plugin: registry + wake queue.

Two files, both under a configurable ``data_dir`` (default
``~/.phoson/monitors/``):

- ``monitors.json`` — the registry (monitor definitions + state). Rewritten
  atomically (tmp + fsync + ``os.replace``) on every mutation, so a crash
  mid-write can never corrupt an existing registry.
- ``wake.jsonl`` — the queue of fired events, one JSON object per line.
  Appended one line at a time (fsynced) so a crash loses at most the
  in-flight line; consumers read with a lenient parser that skips a
  truncated tail line instead of failing.

Durability model (documented, deliberate): the *disk* is the source of
truth; in-memory tasks are a cache re-derived from it. A process that dies
leaves its monitors in state ``running``, and the next host
(``initialize()``/``ensure_started()``) resurrects them. Pending wakes
survive restarts and are drained by whichever host takes over.

All public APIs are sync by design: the plugin hops to a worker thread via
``asyncio.to_thread`` when it needs disk access (same pattern as
``phoson_agent.sessions.storage_jsonl.JsonlStorage``), so the host event
loop is never blocked by I/O.
"""

import os
import json
import uuid
import logging
import datetime
from pathlib import Path
from dataclasses import field, asdict, dataclass

logger = logging.getLogger(__name__)

_REGISTRY_FILE = "monitors.json"
_WAKE_FILE = "wake.jsonl"
_FORMAT_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _parse_iso(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


@dataclass
class MonitorDef:
    """One registered monitor (definition + runtime state).

    Args:
        name: Unique monitor name (the key in the registry).
        kind: Monitor kind (``interval``, ``file``, ``command``).
        spec: Kind-specific parameters as a JSON object.
        state: ``running`` or ``stopped``.
        session_id: Session that registered the monitor; wakes carry it so
            a host can resume the same conversation tree. Empty when the
            host did not provide one.
        created_at: ISO-8601 UTC registration time.
        last_fired_at: ISO-8601 UTC of the last fire (or None).
        last_check_at: ISO-8601 UTC of the last poll/check (or None).
        fire_count: Total number of fires since registration.
    """

    name: str
    kind: str
    spec: dict = field(default_factory=dict)
    state: str = "running"
    session_id: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    last_fired_at: str | None = None
    last_check_at: str | None = None
    fire_count: int = 0

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["version"] = _FORMAT_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MonitorDef":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class WakeEvent:
    """One fired monitor event, waiting for a host to consume it.

    Attributes:
        id: Unique event id (hex).
        monitor: Name of the monitor that fired.
        kind: Monitor kind (``interval``/``file``/``command``/...).
        session_id: Original session the monitor was registered for; the
            host should resume this conversation tree when acting on it.
        fired_at: ISO-8601 UTC fire time.
        payload: Kind-specific fire data (free JSON object).
        consumed: True once a host has turned it into input.
    """

    id: str
    monitor: str
    kind: str
    session_id: str
    fired_at: str
    payload: dict = field(default_factory=dict)
    consumed: bool = False

    @classmethod
    def create(
        cls, monitor: str, kind: str, session_id: str, payload: dict | None = None
    ) -> "WakeEvent":
        return cls(
            id=uuid.uuid4().hex[:12],
            monitor=monitor,
            kind=kind,
            session_id=session_id or "",
            fired_at=_utc_now_iso(),
            payload=dict(payload or {}),
            consumed=False,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WakeEvent":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        values = {k: v for k, v in data.items() if k in known}
        payload = values.get("payload")
        values["payload"] = payload if isinstance(payload, dict) else {}
        return cls(**values)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically (tmp + fsync + replace).

    Writes a sibling ``<name>.tmp.<pid>``, fsyncs, then ``os.replace`` —
    the same strategy as ``JsonlStorage._save_sync``. A crashed write can
    leave a tmp orphan, which the next write of the same file cleans up.
    """
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    wrote_ok = False
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        wrote_ok = True
    finally:
        if not wrote_ok:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove temp file %s", tmp_path)


def _remove_stale_tmp(path: Path) -> None:
    """Remove any ``<name>.tmp.*`` orphans from a crashed previous write."""
    try:
        for orphan in path.parent.glob(f"{path.name}.tmp.*"):
            orphan.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not scan for stale tmp files near %s", path)


class MonitorStore:
    """The monitor registry (``monitors.json``), the source of truth.

    Args:
        data_dir: Directory holding the state files (created on demand).
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.data_dir / _REGISTRY_FILE
        _remove_stale_tmp(self._registry_path)
        self._monitors: dict[str, MonitorDef] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, name: str) -> MonitorDef | None:
        """Return the monitor with ``name`` or None."""
        return self._monitors.get(name)

    def list(self) -> list[MonitorDef]:
        """All monitors, newest registration first."""
        return sorted(
            self._monitors.values(),
            key=lambda m: m.created_at,
            reverse=True,
        )

    def add(self, monitor: MonitorDef) -> None:
        """Insert a new monitor. Raises ValueError on duplicate name."""
        if monitor.name in self._monitors:
            raise ValueError(f"Monitor {monitor.name!r} already exists.")
        self._monitors[monitor.name] = monitor
        self._save()

    def replace(self, monitor: MonitorDef) -> None:
        """Replace a monitor by name (state transitions, stop/start).

        Raises KeyError when the name is unknown.
        """
        if monitor.name not in self._monitors:
            raise KeyError(f"Monitor {monitor.name!r} does not exist.")
        self._monitors[monitor.name] = monitor
        self._save()

    def remove(self, name: str) -> MonitorDef:
        """Delete a monitor by name. Raises KeyError when unknown."""
        monitor = self._monitors.pop(name, None)
        if monitor is None:
            raise KeyError(f"Monitor {name!r} does not exist.")
        self._save()
        return monitor

    def mark_fired(self, name: str, when: str | None = None) -> MonitorDef | None:
        """Bump ``last_fired_at``/``fire_count`` and persist.

        Returns the updated monitor, or None when unknown.
        """
        monitor = self._monitors.get(name)
        if monitor is None:
            return None
        updated = MonitorDef(
            name=monitor.name,
            kind=monitor.kind,
            spec=monitor.spec,
            state=monitor.state,
            session_id=monitor.session_id,
            created_at=monitor.created_at,
            last_fired_at=when or _utc_now_iso(),
            last_check_at=monitor.last_check_at,
            fire_count=monitor.fire_count + 1,
        )
        self._monitors[name] = updated
        self._save()
        return updated

    def mark_checked(self, name: str, when: str | None = None) -> None:
        """Bump ``last_check_at`` without persisting (hot path).

        Callers should persist periodically (``flush``) or on stop; a lost
        ``last_check_at`` is cosmetic, never data loss.
        """
        monitor = self._monitors.get(name)
        if monitor is None:
            return
        updated = MonitorDef(
            name=monitor.name,
            kind=monitor.kind,
            spec=monitor.spec,
            state=monitor.state,
            session_id=monitor.session_id,
            created_at=monitor.created_at,
            last_fired_at=monitor.last_fired_at,
            last_check_at=when or _utc_now_iso(),
            fire_count=monitor.fire_count,
        )
        self._monitors[name] = updated

    def flush(self) -> None:
        """Persist the in-memory registry (e.g. after hot-path updates)."""
        self._save()

    # ── Internal ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._registry_path.exists():
            return
        try:
            raw = self._registry_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt monitor registry %s — starting empty: %s",
                self._registry_path,
                exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                "Monitor registry %s is not an object — starting empty.",
                self._registry_path,
            )
            return
        for name, entry in (data.get("monitors") or {}).items():
            try:
                self._monitors[name] = MonitorDef.from_dict(entry)
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping corrupt registry entry %r: %s", name, exc)

    def _save(self) -> None:
        data = {
            "version": _FORMAT_VERSION,
            "monitors": {name: m.to_dict() for name, m in self._monitors.items()},
        }
        _atomic_write_json(self._registry_path, data)


class WakeQueue:
    """The pending-wake queue (``wake.jsonl``).

    Every mutation rewrites the file atomically (tmp + fsync + replace):
    the queue is bounded by the per-monitor cap, so a rewrite is cheap,
    and it keeps the on-disk queue exactly equal to the in-memory one
    (no resurrected dropped events, no truncated lines to parse — though
    the reader stays lenient anyway).

    Args:
        data_dir: Directory holding the state files.
        max_pending_per_monitor: Pending events kept per monitor before the
            oldest one is dropped (anti-storm). New fires are never lost;
            the note about dropped events goes into the new fire's payload.
    """

    def __init__(self, data_dir: str | Path, max_pending_per_monitor: int = 5) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._wake_path = self.data_dir / _WAKE_FILE
        _remove_stale_tmp(self._wake_path)
        self.max_pending_per_monitor = max(1, int(max_pending_per_monitor))
        self._events: list[WakeEvent] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def append(self, event: WakeEvent) -> WakeEvent | None:
        """Append a fire; enforce the per-monitor pending cap.

        Returns the event actually persisted (the new one, possibly with a
        ``dropped_previous`` note), or None when an identical unconsumed
        event for the same monitor is already pending (dedupe on
        back-to-back fires of the same monitor).
        """
        for existing in self._events:
            if (
                not existing.consumed
                and existing.monitor == event.monitor
                and existing.payload == event.payload
            ):
                logger.debug(
                    "Deduped wake for monitor %r (identical pending event).",
                    event.monitor,
                )
                return None

        dropped = 0
        pending = [
            e for e in self._events if not e.consumed and e.monitor == event.monitor
        ]
        while len(pending) + 1 > self.max_pending_per_monitor:
            oldest = pending.pop(0)
            dropped += 1
            self._events.remove(oldest)

        if dropped:
            event = WakeEvent(
                id=event.id,
                monitor=event.monitor,
                kind=event.kind,
                session_id=event.session_id,
                fired_at=event.fired_at,
                payload={**event.payload, "dropped_previous": dropped},
            )
        self._events.append(event)
        # Rewrite the whole queue atomically: the per-monitor cap may have
        # dropped an event that was already on disk, and a plain append
        # would resurrect it on the next load. The queue is bounded (cap *
        # monitors), so a rewrite is cheap.
        self._rewrite([e.to_dict() for e in self._events])
        return event

    def pending(self, session_id: str | None = None) -> list[WakeEvent]:
        """Unconsumed events, oldest first.

        ``session_id=None`` returns everything; otherwise only events for
        that session.
        """
        return [
            e
            for e in self._events
            if not e.consumed and (session_id is None or e.session_id == session_id)
        ]

    def pending_count(self, session_id: str | None = None) -> int:
        return len(self.pending(session_id))

    def consume(
        self, ids: list[str] | tuple[str, ...] | None = None
    ) -> list[WakeEvent]:
        """Mark events consumed and rewrite the queue file atomically.

        ``ids=None`` consumes every pending event. Returns the consumed
        events (already stripped of the ``consumed`` flag semantics — the
        caller uses them as the wake message content).
        """
        wanted = set(ids) if ids is not None else None
        consumed: list[WakeEvent] = []
        remaining: list[WakeEvent] = []
        for event in self._events:
            is_target = (wanted is None or event.id in wanted) and not event.consumed
            if is_target:
                consumed.append(event)
            else:
                remaining.append(event)
        self._events = remaining
        self._rewrite([e.to_dict() for e in self._events])
        return consumed

    def peek_all(self) -> list[WakeEvent]:
        """Every event (pending and consumed) for diagnostics."""
        return list(self._events)

    # ── Internal ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._wake_path.exists():
            return
        try:
            lines = self._wake_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("Could not read wake queue %s: %s", self._wake_path, exc)
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                self._events.append(WakeEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                # A truncated tail line (crash mid-append) is expected and
                # safe to skip; the monitor simply fires again.
                logger.warning("Skipping malformed wake line: %s", exc)

    def _rewrite(self, lines: list[dict]) -> None:
        # Rewrite the queue atomically: consumed events disappear from
        # disk, pending ones survive a crash.
        tmp_path = self._wake_path.with_name(
            f"{self._wake_path.name}.tmp.{os.getpid()}"
        )
        wrote_ok = False
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                for line in lines:
                    f.write(json.dumps(line, ensure_ascii=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._wake_path)
            wrote_ok = True
        finally:
            if not wrote_ok:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove temp file %s", tmp_path)


__all__ = [
    "MonitorDef",
    "MonitorStore",
    "WakeEvent",
    "WakeQueue",
]
