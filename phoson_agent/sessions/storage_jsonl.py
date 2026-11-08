import os
import sys
import json
import asyncio
import logging
import weakref
import datetime
import tempfile
import contextlib
import unicodedata
from pathlib import Path, PureWindowsPath
from dataclasses import field, dataclass

from phoson_agent.exceptions import PhosonSessionNotFoundError
from phoson_agent.sessions.models import (
    STATUS_ACTIVE,
    SessionMeta,
    SessionStorage,
    ConversationTree,
)
from phoson_agent.sessions.serialization import (
    node_to_dict,
    node_from_dict,
    apply_tree_meta,
    tree_meta_to_dict,
)

logger = logging.getLogger(__name__)


def _validate_session_id(session_id: str) -> None:
    """Require an ID that is safe and portable as one filename stem."""
    if (
        not isinstance(session_id, str)
        or not session_id
        or session_id in {".", ".."}
        or any(
            char in '<>:"/\\|?*' or unicodedata.category(char) == "Cc"
            for char in session_id
        )
        or session_id[-1] in {".", " "}
        or PureWindowsPath(session_id).name != session_id
        or PureWindowsPath(f"{session_id}.jsonl").is_reserved()
        or Path(f"{session_id}.jsonl").stem != session_id
    ):
        raise ValueError("session_id must be a safe, non-empty filename stem")


@dataclass
class JsonlStorage(SessionStorage):
    """JSONL-based session storage for conversation trees.

    Each session is stored as a single JSONL file where each line is a JSON
    object. The first line (optional) contains session metadata, followed by
    node records.

    Durability guarantees:

    - **Atomic writes**: ``save`` writes to a temporary file alongside the
      target and then ``os.replace``-s it into place. A crash mid-write
      cannot corrupt an existing session file.
    - **Non-blocking**: every disk operation runs through
      ``asyncio.to_thread`` so callers awaiting on a busy event loop are
      not blocked by I/O.
    - **Serialized per session**: saves of one session through one storage
      instance never overlap, and ``save_meta``'s load-modify-save runs as
      one unit, so a metadata update cannot write back a tree that predates
      a concurrent checkpoint. Temporary files are unique per write.
    - **Private**: session files are created ``0o600`` (they hold tool
      output, which may include secrets) and a newly created session
      directory is ``0o700``.
    - **Multi-process safe**: each save holds an advisory lock on a side
      file and, if another process saved the session since this instance
      last read or wrote it, merges that process's nodes in (they become
      another branch) instead of overwriting them.

    Args:
        base_path: Directory path where session JSONL files are stored.

    Example:
        storage = JsonlStorage(base_path=Path("./sessions"))
        await storage.save(tree)
        sessions = await storage.list_sessions()
    """

    base_path: Path
    _locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = field(  # noqa: E501
        default_factory=weakref.WeakKeyDictionary, init=False, repr=False, compare=False
    )

    #: (mtime_ns, size) of each session file as this instance last read or
    #: wrote it. A different stamp at save time means another process wrote
    #: the session in between, and its nodes are merged in (R4).
    _stamps: dict[str, "tuple[int, int] | None"] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Create the base directory if it doesn't exist."""
        self.base_path.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _lock(self, session_id: str) -> asyncio.Lock:
        # Keyed by event loop too: an asyncio.Lock binds to the loop it is
        # first contended on, and hosts may drive one storage from several
        # ``asyncio.run`` calls (one-shot, ``bg list``).
        loop = asyncio.get_running_loop()
        per_loop = self._locks.get(loop)
        if per_loop is None:
            per_loop = self._locks[loop] = {}
        lock = per_loop.get(session_id)
        if lock is None:
            lock = per_loop[session_id] = asyncio.Lock()
        return lock

    def _session_file(self, session_id: str) -> Path:
        """Get the file path for a session."""
        _validate_session_id(session_id)
        return self.base_path / f"{session_id}.jsonl"

    # ── Public async API ────────────────────────────────────────────────

    async def save(self, tree: ConversationTree) -> None:
        """Persist a conversation tree to a JSONL file atomically."""
        async with self._lock(tree.session_id):
            await asyncio.to_thread(self._save_sync, tree)

    async def load(self, session_id: str) -> ConversationTree:
        """Load a conversation tree from a JSONL file."""
        return await asyncio.to_thread(self._load_sync, session_id)

    async def list_sessions(self, cwd: str | None = None) -> list[SessionMeta]:
        """List available sessions, most recently updated first.

        When *cwd* is given, only sessions started in that working directory
        are returned (plus legacy sessions with no recorded cwd) — #212.
        """
        return await asyncio.to_thread(self._list_sessions_sync, cwd)

    async def delete(self, session_id: str) -> None:
        """Delete a session file."""
        await asyncio.to_thread(self._delete_sync, session_id)

    async def save_meta(self, session_id: str, meta: dict) -> None:
        """Update session metadata and save.

        ``status`` and ``last_run_id`` (#129) are passed through from the
        meta dict when present; callers that do not manage run status
        (legacy metrics dicts) leave the tree's current values untouched.
        Only the keys present in *meta* are applied: a partial update such as
        ``{"title": ...}`` leaves the persisted cost/token/step totals as they
        are (previously absent keys were written back as ``0``).
        """
        async with self._lock(session_id):
            tree = await asyncio.to_thread(self._load_sync, session_id)
            tree.update_session_meta(**_meta_updates(meta))
            await asyncio.to_thread(self._save_sync, tree)

    async def save_with_meta(self, tree: ConversationTree, meta: dict) -> None:
        """Apply ``meta`` to ``tree`` and persist both in **one** write.

        ``save`` followed by ``save_meta`` wrote the whole file, then read and
        parsed it back, then wrote it again — three passes over the session on
        every checkpoint (R5). The tree already holds the meta fields, so
        updating them in memory and writing once is equivalent.
        """
        tree.update_session_meta(**_meta_updates(meta))
        await self.save(tree)

    async def list_meta(self, cwd: str | None = None) -> list[SessionMeta]:
        return await self.list_sessions(cwd=cwd)

    # ── Sync internals (run inside ``asyncio.to_thread``) ───────────────

    def _save_sync(self, tree: ConversationTree) -> None:
        """Atomically write the tree to disk.

        Strategy: write to a unique temporary file (``mkstemp``, mode
        ``0o600``) in the same directory, fsync, then ``os.replace`` to swap
        it in. ``os.replace`` is atomic on POSIX and Windows when source and
        destination live on the same filesystem (same parent directory). The
        unique name keeps concurrent writers — other threads, other storage
        instances, other processes — from deleting or clobbering each other's
        temporary file.
        """
        file_path = self._session_file(tree.session_id)
        with _file_lock(file_path):
            self._merge_foreign_nodes(tree, file_path)
            self._write_atomic(tree, file_path)
            self._stamps[tree.session_id] = _stamp(file_path)

    def _merge_foreign_nodes(self, tree: ConversationTree, file_path: Path) -> None:
        """Fold in nodes another process saved since we last read or wrote.

        Two CLIs on the same session used to overwrite each other: the
        second save dropped the first one's turns (R4). Node ids are unique
        and nodes immutable, so the union is the correct state — the other
        process's turns become another branch of this tree. The check is a
        ``stat`` against the stamp of our own last read/write, so the common
        single-writer case never re-reads the file.
        """
        current = _stamp(file_path)
        if current is None or current == self._stamps.get(tree.session_id):
            return
        try:
            disk = self._load_sync(tree.session_id, stamp=False)
        except Exception:  # noqa: BLE001 - a damaged file must not block saving
            logger.warning("Could not read %s to merge it", file_path, exc_info=True)
            return
        foreign = [n for n in disk.nodes.values() if n.id not in tree.nodes]
        added = 0
        for node in sorted(foreign, key=lambda n: n.created_at):
            if node.parent_id is None or node.parent_id in tree.nodes:
                tree.add_node(node)
                added += 1
        if added:
            logger.info(
                "Merged %d node(s) another process saved to session %s",
                added,
                tree.session_id,
            )

    def _write_atomic(self, tree: ConversationTree, file_path: Path) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=file_path.parent, prefix=f".{file_path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)

        nodes = sorted(tree.nodes.values(), key=lambda n: n.created_at)
        wrote_ok = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(tree_meta_to_dict(tree), ensure_ascii=True))
                f.write("\n")
                for node in nodes:
                    f.write(json.dumps(node_to_dict(node), ensure_ascii=True))
                    f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
            wrote_ok = True
        finally:
            if not wrote_ok:
                # Do not leave a stale tmp file behind on any failure path.
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("Could not remove temp file %s: %s", tmp_path, exc)

    def _load_sync(self, session_id: str, *, stamp: bool = True) -> ConversationTree:
        file_path = self._session_file(session_id)
        if stamp:
            # What we are about to read is the version later saves compare to.
            self._stamps[session_id] = _stamp(file_path)
        if not file_path.exists():
            raise PhosonSessionNotFoundError(
                f"Session {session_id} does not exist.",
                session_id=session_id,
            )

        tree = ConversationTree.new(session_id=session_id)
        with file_path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # One damaged line (hand edit, external truncation) must
                    # not make the whole session unloadable; the listing
                    # already tolerates it the same way.
                    logger.warning(
                        "Skipping malformed line %d in %s: %s",
                        line_number,
                        file_path,
                        exc,
                    )
                    continue
                if data.get("type") == "session_meta":
                    apply_tree_meta(tree, data)
                    continue
                tree.add_node(node_from_dict(data))
        return tree

    def _list_sessions_sync(self, cwd: str | None = None) -> list[SessionMeta]:
        return list_session_metas(self.base_path, cwd=cwd)

    def _delete_sync(self, session_id: str) -> None:
        file_path = self._session_file(session_id)
        file_path.unlink(missing_ok=True)
        _lock_path(file_path).unlink(missing_ok=True)
        self._stamps.pop(session_id, None)


def _stamp(file_path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of a session file, or ``None`` when it is missing."""
    try:
        info = file_path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _lock_path(file_path: Path) -> Path:
    return file_path.with_name(f".{file_path.name}.lock")


@contextlib.contextmanager
def _file_lock(file_path: Path):
    """Exclusive cross-process lock around one read-merge-write of a session.

    Advisory (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows) on a
    side file next to the session, so it never blocks readers of the session
    file itself. Best effort: if locking is unavailable the write proceeds.
    """
    lock = _lock_path(file_path)
    try:
        handle = open(lock, "a+b")  # noqa: SIM115 - held for the block
    except OSError:
        yield
        return
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            logger.debug("Could not lock %s; writing without it", lock)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            # flock is released when the handle closes.
        finally:
            handle.close()


def _meta_updates(meta: dict) -> dict:
    """Map a ``save_meta`` dict to ``update_session_meta`` kwargs.

    Absent keys map to ``None`` (= keep the stored value). ``total_tokens``
    is only recomputed when at least one side of the split is given.
    """
    updates: dict = {}
    if "total_cost_usd" in meta:
        updates["total_cost"] = float(meta["total_cost_usd"])
    has_input = "total_input_tokens" in meta
    has_output = "total_output_tokens" in meta
    if has_input:
        updates["total_input_tokens"] = int(meta["total_input_tokens"])
    if has_output:
        updates["total_output_tokens"] = int(meta["total_output_tokens"])
    if has_input or has_output:
        updates["total_tokens"] = int(meta.get("total_input_tokens", 0)) + int(
            meta.get("total_output_tokens", 0)
        )
    if "step_count" in meta:
        updates["step_count"] = int(meta["step_count"])
    if meta.get("last_model"):
        updates["last_model"] = meta["last_model"]
    for key in ("title", "status", "last_run_id"):
        if meta.get(key) is not None:
            updates[key] = meta[key]
    return updates


def list_session_metas(base_path: Path, cwd: str | None = None) -> list[SessionMeta]:
    """List session metas in *base_path*, most recently updated first.

    Public, storage-instance-free variant of
    :meth:`JsonlStorage._list_sessions_sync` — used by ``phoson-cli bg
    list`` (#129), which must read the session directory without
    instantiating a storage (which would create the directory as a side
    effect). A missing directory yields an empty list.

    When *cwd* is given, only sessions started in that working directory are
    returned, plus legacy sessions with no recorded ``cwd`` (treated as
    global) — #212 per-directory session scoping.
    """
    sessions: list[SessionMeta] = []
    if not Path(base_path).is_dir():
        return sessions
    for file_path in sorted(Path(base_path).glob("*.jsonl")):
        session_id = file_path.name.removesuffix(".jsonl")
        try:
            _validate_session_id(session_id)
        except ValueError:
            logger.warning("Skipping session file with unsafe id: %s", file_path.name)
            continue
        meta = _read_session_meta(file_path)
        if meta is not None:
            # A session with no recorded cwd (legacy/global) is shown from
            # every directory; a scoped one only from its own.
            if cwd is not None and meta.cwd not in (None, cwd):
                continue
            sessions.append(meta)

    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def _read_meta_fast(
    file_path: Path,
) -> "tuple[dict, datetime.datetime | None, int] | None":
    """Read a session's meta without decoding every record.

    Files written by ``save`` start with the ``session_meta`` line, followed
    by node records in creation order: only the first two lines need JSON
    parsing, the rest are just counted. The picker and the startup cache
    list every session, and decoding each whole session file there grew
    with every turn of every session (R5). Returns ``None`` when the file
    does not start with a meta record (legacy layout: full scan).
    """
    try:
        with file_path.open("rb") as f:
            first = f.readline()
            try:
                meta = json.loads(first)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            if not isinstance(meta, dict) or meta.get("type") != "session_meta":
                return None
            created_at: datetime.datetime | None = None
            count = 0
            for line in f:
                if not line.strip():
                    continue
                count += 1
                if count == 1:
                    try:
                        raw_ts = json.loads(line).get("created_at")
                        created_at = (
                            datetime.datetime.fromisoformat(raw_ts) if raw_ts else None
                        )
                    except (
                        json.JSONDecodeError,
                        TypeError,
                        ValueError,
                        AttributeError,
                    ):
                        created_at = None
            return meta, created_at, count
    except OSError:
        return None


def _read_session_meta(file_path: Path) -> SessionMeta | None:
    """Read just enough of a JSONL session file to build its ``SessionMeta``.

    Each line is parsed exactly once, in order. The first non-meta record
    determines ``created_at``; ``message_count`` is the number of non-meta
    records. Cost/token/step/model totals come from the ``session_meta``
    record persisted by ``save_meta`` (last one wins; files are rewritten
    atomically, so there is normally exactly one). Empty or malformed
    files are skipped silently.
    """
    created_at: datetime.datetime | None = None
    message_count = 0
    meta_values: dict | None = None
    fast = _read_meta_fast(file_path)
    if fast is not None:
        meta_values, created_at, message_count = fast
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f if fast is None else ():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    logger.debug("Skipping malformed line in %s: %s", file_path, exc)
                    continue
                if record.get("type") == "session_meta":
                    meta_values = record
                    continue
                message_count += 1
                if created_at is None:
                    raw_ts = record.get("created_at")
                    if raw_ts is not None:
                        try:
                            created_at = datetime.datetime.fromisoformat(raw_ts)
                        except (TypeError, ValueError):
                            created_at = None
    except OSError:
        return None

    if message_count == 0 or created_at is None:
        return None

    stat = file_path.stat()
    updated_at = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.UTC)
    has_split = meta_values and (
        "total_input_tokens" in meta_values or "total_output_tokens" in meta_values
    )
    input_tokens = int(meta_values.get("total_input_tokens", 0)) if meta_values else 0
    output_tokens = int(meta_values.get("total_output_tokens", 0)) if meta_values else 0
    if meta_values and not has_split:
        # F-34 legacy: only the sum was persisted — surface it under output.
        output_tokens = int(meta_values.get("total_tokens", 0))
    # #129: legacy files carry no status — default to "active" so they are
    # treated as "may have died mid-run" (same rule as apply_tree_meta).
    status = (
        meta_values.get("status") if meta_values and meta_values.get("status") else None
    ) or STATUS_ACTIVE
    return SessionMeta(
        id=file_path.name.removesuffix(".jsonl"),
        created_at=created_at,
        updated_at=updated_at,
        message_count=message_count,
        total_cost=(float(meta_values.get("total_cost", 0.0)) if meta_values else 0.0),
        total_tokens=int(meta_values.get("total_tokens", 0)) if meta_values else 0,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        step_count=int(meta_values.get("step_count", 0)) if meta_values else 0,
        last_model=meta_values.get("last_model") if meta_values else None,
        title=meta_values.get("title") if meta_values else None,
        status=status,
        last_run_id=meta_values.get("last_run_id") if meta_values else None,
        cwd=meta_values.get("cwd") if meta_values else None,
    )
