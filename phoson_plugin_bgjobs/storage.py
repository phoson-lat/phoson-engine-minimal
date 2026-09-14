"""Persistent state for the background-jobs plugin: registry + wake queue.

Two files under a configurable ``data_dir`` (default ``~/.phoson/bgjobs/``):

- ``jobs.json`` — the registry (job definitions + runtime state). Rewritten
  atomically (tmp + fsync + ``os.replace``) on every mutation, so a crash
  mid-write can never corrupt an existing registry.
- ``wakes.jsonl`` — the queue of completed-job events, one JSON object per
  line. Rewritten atomically on every mutation so the on-disk queue equals
  the in-memory one (no resurrected consumed events).

Durability model (same as ``phoson_plugin_monitor``): the *disk* is the
source of truth; in-memory asyncio tasks are a cache re-derived from it. A
process that dies leaves its jobs in state ``running``; the next host
(``initialize()``/``ensure_started()``) reconciles them.

All public APIs are sync by design: the plugin hops to a worker thread via
``asyncio.to_thread`` when it needs disk access, so the host event loop is
never blocked by I/O.
"""

import os
import json
import uuid
import logging
import datetime
from pathlib import Path
from dataclasses import field, asdict, dataclass

logger = logging.getLogger(__name__)

_REGISTRY_FILE = "jobs.json"
_WAKE_FILE = "wakes.jsonl"
_FORMAT_VERSION = 1

# Job states:
#   running   — spawned, not yet reaped by the owning process
#   completed — exited on its own (returncode recorded; 0 == success)
#   stopped   — killed via stop_bg_job (process group)
#   orphaned  — a previous process died while the job ran; we cannot reap it
JOB_STATES = ("running", "completed", "stopped", "orphaned")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


@dataclass
class JobDef:
    """One background job (definition + runtime state).

    Args:
        job_id: Unique id (hex), the key in the registry.
        name: Human-friendly job name (not necessarily unique).
        command: The shell command that was launched (verbatim).
        cwd: Working directory used for the spawn ("" = host cwd).
        session_id: Session that launched the job; wakes carry it so a host
            can resume the same conversation tree. Empty when unbound.
        state: One of ``JOB_STATES``.
        pid: Child pid, when known.
        pgid: Child process-group id (``start_new_session``), when known.
        created_at: ISO-8601 UTC launch time.
        finished_at: ISO-8601 UTC completion time (or None).
        returncode: Exit code once reaped (or None while running).
        timed_out: True when the job was killed by its watchdog.
        duration_ms: Wall-clock duration in milliseconds (or None).
        output_tail: Tail of the combined stdout/stderr (bounded).
        max_output_chars: Cap on ``output_tail``.
        log_path: Absolute path of the full combined output log.
    """

    job_id: str
    name: str
    command: str
    cwd: str = ""
    session_id: str = ""
    state: str = "running"
    pid: int | None = None
    pgid: int | None = None
    created_at: str = field(default_factory=_utc_now_iso)
    finished_at: str | None = None
    returncode: int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    output_tail: str = ""
    max_output_chars: int = 4000
    log_path: str = ""

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["version"] = _FORMAT_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "JobDef":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class WakeEvent:
    """One completed-job event, waiting for a host to consume it.

    Attributes:
        id: Unique event id (hex).
        job_id: Id of the job that finished.
        name: Human-friendly job name.
        session_id: Original session the job was launched from; the host
            should resume this conversation tree when acting on the event.
        fired_at: ISO-8601 UTC fire time.
        payload: Completion data (returncode, output_tail, duration_ms, ...).
        consumed: True once a host has turned it into input.
    """

    id: str
    job_id: str
    name: str
    session_id: str
    fired_at: str
    payload: dict = field(default_factory=dict)
    consumed: bool = False

    @classmethod
    def create(
        cls,
        job_id: str,
        name: str,
        session_id: str,
        payload: dict | None = None,
    ) -> "WakeEvent":
        return cls(
            id=uuid.uuid4().hex[:12],
            job_id=job_id,
            name=name,
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
    """Write ``data`` to ``path`` atomically (tmp + fsync + replace)."""
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


class JobStore:
    """The job registry (``jobs.json``), the source of truth.

    Args:
        data_dir: Directory holding the state files (created on demand).
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.data_dir / _REGISTRY_FILE
        _remove_stale_tmp(self._registry_path)
        self._jobs: dict[str, JobDef] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, job_id: str) -> JobDef | None:
        """Return the job with ``job_id`` or None."""
        return self._jobs.get(job_id)

    def list(self) -> list[JobDef]:
        """All jobs, newest launch first."""
        return sorted(
            self._jobs.values(),
            key=lambda j: j.created_at,
            reverse=True,
        )

    def add(self, job: JobDef) -> None:
        """Insert a new job. Raises ValueError on duplicate id."""
        if job.job_id in self._jobs:
            raise ValueError(f"Job {job.job_id!r} already exists.")
        self._jobs[job.job_id] = job
        self._save()

    def replace(self, job: JobDef) -> None:
        """Replace a job by id (state transitions). Raises KeyError if unknown."""
        if job.job_id not in self._jobs:
            raise KeyError(f"Job {job.job_id!r} does not exist.")
        self._jobs[job.job_id] = job
        self._save()

    def remove(self, job_id: str) -> JobDef:
        """Delete a job by id. Raises KeyError when unknown."""
        job = self._jobs.pop(job_id, None)
        if job is None:
            raise KeyError(f"Job {job_id!r} does not exist.")
        self._save()
        return job

    # ── Internal ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._registry_path.exists():
            return
        try:
            raw = self._registry_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt job registry %s — starting empty: %s",
                self._registry_path,
                exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                "Job registry %s is not an object — starting empty.",
                self._registry_path,
            )
            return
        for job_id, entry in (data.get("jobs") or {}).items():
            try:
                self._jobs[job_id] = JobDef.from_dict(entry)
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping corrupt registry entry %r: %s", job_id, exc)

    def _save(self) -> None:
        data = {
            "version": _FORMAT_VERSION,
            "jobs": {jid: j.to_dict() for jid, j in self._jobs.items()},
        }
        _atomic_write_json(self._registry_path, data)


class WakeQueue:
    """The pending-wake queue (``wakes.jsonl``).

    Every mutation rewrites the file atomically (tmp + fsync + replace). The
    queue is bounded by the per-job cap, so a rewrite is cheap.

    Args:
        data_dir: Directory holding the state files.
        max_pending_per_job: Pending events kept per job before the oldest is
            dropped (anti-storm). New fires are never lost; the note about
            dropped events goes into the new fire's payload.
    """

    def __init__(self, data_dir: str | Path, max_pending_per_job: int = 5) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._wake_path = self.data_dir / _WAKE_FILE
        _remove_stale_tmp(self._wake_path)
        self.max_pending_per_job = max(1, int(max_pending_per_job))
        self._events: list[WakeEvent] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def append(self, event: WakeEvent) -> WakeEvent | None:
        """Append a fire; enforce the per-job pending cap.

        Returns the event actually persisted (the new one, possibly with a
        ``dropped_previous`` note), or None when an identical unconsumed
        event for the same job is already pending (dedupe).
        """
        for existing in self._events:
            if (
                not existing.consumed
                and existing.job_id == event.job_id
                and existing.payload == event.payload
            ):
                logger.debug(
                    "Deduped wake for job %r (identical pending event).",
                    event.job_id,
                )
                return None

        dropped = 0
        pending = [
            e for e in self._events if not e.consumed and e.job_id == event.job_id
        ]
        while len(pending) + 1 > self.max_pending_per_job:
            oldest = pending.pop(0)
            dropped += 1
            self._events.remove(oldest)

        if dropped:
            event = WakeEvent(
                id=event.id,
                job_id=event.job_id,
                name=event.name,
                session_id=event.session_id,
                fired_at=event.fired_at,
                payload={**event.payload, "dropped_previous": dropped},
            )
        self._events.append(event)
        self._rewrite([e.to_dict() for e in self._events])
        return event

    def pending(self, session_id: str | None = None) -> list[WakeEvent]:
        """Unconsumed events, oldest first (``None`` = all sessions)."""
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
        """Mark events consumed and rewrite the queue file atomically."""
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
                logger.warning("Skipping malformed wake line: %s", exc)

    def _rewrite(self, lines: list[dict]) -> None:
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
    "JobDef",
    "JobStore",
    "WakeEvent",
    "WakeQueue",
    "JOB_STATES",
]
