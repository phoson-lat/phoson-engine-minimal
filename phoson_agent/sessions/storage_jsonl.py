import os
import json
import asyncio
import datetime
from pathlib import Path
from dataclasses import dataclass

from phoson_agent.exceptions import PhosonSessionNotFoundError
from phoson_agent.sessions.models import SessionMeta, SessionStorage, ConversationTree
from phoson_agent.sessions.serialization import (
    node_to_dict,
    node_from_dict,
    apply_tree_meta,
    tree_meta_to_dict,
)


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
        """Update session metadata and save."""
        tree = await self.load(session_id)
        tree.update_session_meta(
            total_cost=float(meta.get("total_cost_usd", 0.0)),
            total_tokens=int(meta.get("total_input_tokens", 0))
            + int(meta.get("total_output_tokens", 0)),
            step_count=int(meta.get("step_count", 0)),
            last_model=meta.get("last_model") or None,
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
                except OSError:
                    pass

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
        sessions: list[SessionMeta] = []
        for file_path in sorted(self.base_path.glob("*.jsonl")):
            meta = _read_session_meta(file_path)
            if meta is not None:
                sessions.append(meta)

        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def _delete_sync(self, session_id: str) -> None:
        file_path = self._session_file(session_id)
        file_path.unlink(missing_ok=True)


def _read_session_meta(file_path: Path) -> SessionMeta | None:
    """Read just enough of a JSONL session file to build its ``SessionMeta``.

    Each line is parsed exactly once, in order. The first non-meta record
    determines ``created_at``; ``message_count`` is the number of non-meta
    records. Empty or malformed files are skipped silently.
    """
    created_at: datetime.datetime | None = None
    message_count = 0
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
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
    return SessionMeta(
        id=file_path.stem,
        created_at=created_at,
        updated_at=updated_at,
        message_count=message_count,
    )
