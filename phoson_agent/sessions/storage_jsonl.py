import json
import datetime
from pathlib import Path
from dataclasses import dataclass

from phoson_agent.sessions.models import SessionMeta, SessionStorage, ConversationTree
from phoson_agent.sessions.serialization import node_to_dict, node_from_dict


@dataclass
class JsonlStorage(SessionStorage):
    base_path: Path

    def __post_init__(self) -> None:
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _session_file(self, session_id: str) -> Path:
        return self.base_path / f"{session_id}.jsonl"

    async def save(self, tree: ConversationTree) -> None:
        file_path = self._session_file(tree.session_id)
        nodes = sorted(tree.nodes.values(), key=lambda n: n.created_at)
        with file_path.open("w", encoding="utf-8") as f:
            for node in nodes:
                f.write(json.dumps(node_to_dict(node), ensure_ascii=True))
                f.write("\n")

    async def load(self, session_id: str) -> ConversationTree:
        file_path = self._session_file(session_id)
        if not file_path.exists():
            raise FileNotFoundError(f"Session {session_id} does not exist.")

        tree = ConversationTree.new(session_id=session_id)
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                tree.add_node(node_from_dict(json.loads(raw)))
        return tree

    async def list_sessions(self) -> list[SessionMeta]:
        sessions: list[SessionMeta] = []
        for file_path in sorted(self.base_path.glob("*.jsonl")):
            with file_path.open("r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
                if not lines:
                    continue
                created_data = json.loads(lines[0])
                created_at = datetime.datetime.fromisoformat(created_data["created_at"])
                message_count = len(lines)

            stat = file_path.stat()
            updated_at = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.UTC)
            sessions.append(
                SessionMeta(
                    id=file_path.stem,
                    created_at=created_at,
                    updated_at=updated_at,
                    message_count=message_count,
                )
            )

        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    async def delete(self, session_id: str) -> None:
        file_path = self._session_file(session_id)
        if file_path.exists():
            file_path.unlink()
