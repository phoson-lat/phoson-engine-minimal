import os
import json
import asyncio
import logging
import datetime
from pathlib import Path
from dataclasses import dataclass

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

    Args:
        base_path: Directory path where session JSONL files are stored.

    Example:
        storage = JsonlStorage(base_path=Path("./sessions"))
        await storage.save(tree)
        sessions = await storage.list_sessions()
    """

    base_path: Path

    def __post_init__(self) -> None:
        """Create the base directory if it doesn't exist."""
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _session_file(self, session_id: str) -> Path:
        """Get the file path for a session."""
        return self.base_path / f"{session_id}.jsonl"

    # ── Public async API ────────────────────────────────────────────────

    async def save(self, tree: ConversationTree) -> None:
        """Persist a conversation tree to a JSONL file atomically."""
        await asyncio.to_thread(self._save_sync, tree)

    async def load(self, session_id: str) -> ConversationTree:
        """Load a conversation tree from a JSONL file."""
        return await asyncio.to_thread(self._load_sync, session_id)

    async def list_sessions(self) -> list[SessionMeta]:
        """List all available sessions, most recently updated first."""
        return await asyncio.to_thread(self._list_sessions_sync)

    async def delete(self, session_id: str) -> None:
        """Delete a session file."""
        await asyncio.to_thread(self._delete_sync, session_id)

    async def save_meta(self, session_id: str, meta: dict) -> None:
        """Update session metadata and save.

        ``status`` and ``last_run_id`` (#129) are passed through from the
        meta dict when present; callers that do not manage run status
        (legacy metrics dicts) leave the tree's current values untouched.
        """
        tree = await self.load(session_id)
        tree.update_session_meta(
            total_cost=float(meta.get("total_cost_usd", 0.0)),
            total_tokens=int(meta.get("total_input_tokens", 0))
            + int(meta.get("total_output_tokens", 0)),
            total_input_tokens=int(meta.get("total_input_tokens", 0)),
            total_output_tokens=int(meta.get("total_output_tokens", 0)),
            step_count=int(meta.get("step_count", 0)),
            last_model=meta.get("last_model") or None,
            title=meta.get("title"),
            status=meta.get("status"),
            last_run_id=meta.get("last_run_id"),
        )
        await self.save(tree)

    async def list_meta(self) -> list[SessionMeta]:
        return await self.list_sessions()

    # ── Sync internals (run inside ``asyncio.to_thread``) ───────────────

    def _save_sync(self, tree: ConversationTree) -> None:
        """Atomically write the tree to disk.

        Strategy: write to ``<file>.tmp.<pid>`` in the same directory, fsync,
        then ``os.replace`` to swap it in. ``os.replace`` is atomic on POSIX
        and Windows when source and destination live on the same filesystem
        (which they do because we use the same parent directory).
        """
        file_path = self._session_file(tree.session_id)
        tmp_path = file_path.with_name(f"{file_path.name}.tmp.{os.getpid()}")

        nodes = sorted(tree.nodes.values(), key=lambda n: n.created_at)
        wrote_ok = False
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
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

    def _load_sync(self, session_id: str) -> ConversationTree:
        file_path = self._session_file(session_id)
        if not file_path.exists():
            raise PhosonSessionNotFoundError(
                f"Session {session_id} does not exist.",
                session_id=session_id,
            )

        tree = ConversationTree.new(session_id=session_id)
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                data = json.loads(raw)
                if data.get("type") == "session_meta":
                    apply_tree_meta(tree, data)
                    continue
                tree.add_node(node_from_dict(data))
        return tree

    def _list_sessions_sync(self) -> list[SessionMeta]:
        return list_session_metas(self.base_path)

    def _delete_sync(self, session_id: str) -> None:
        file_path = self._session_file(session_id)
        file_path.unlink(missing_ok=True)


def list_session_metas(base_path: Path) -> list[SessionMeta]:
    """List session metas in *base_path*, most recently updated first.

    Public, storage-instance-free variant of
    :meth:`JsonlStorage._list_sessions_sync` — used by ``phoson-cli bg
    list`` (#129), which must read the session directory without
    instantiating a storage (which would create the directory as a side
    effect). A missing directory yields an empty list.
    """
    sessions: list[SessionMeta] = []
    if not Path(base_path).is_dir():
        return sessions
    for file_path in sorted(Path(base_path).glob("*.jsonl")):
        meta = _read_session_meta(file_path)
        if meta is not None:
            sessions.append(meta)

    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


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
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
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
        id=file_path.stem,
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
    )
