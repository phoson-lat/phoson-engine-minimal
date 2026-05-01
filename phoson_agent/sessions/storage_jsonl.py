import json
import datetime
from pathlib import Path
from dataclasses import dataclass

from phoson_agent.sessions.models import SessionMeta, SessionStorage, ConversationTree
from phoson_agent.sessions.serialization import (
    node_to_dict,
    node_from_dict,
    apply_tree_meta,
    tree_meta_to_dict,
)


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
            f.write(json.dumps(tree_meta_to_dict(tree), ensure_ascii=True))
            f.write("\n")
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
                data = json.loads(raw)
                if data.get("type") == "session_meta":
                    apply_tree_meta(tree, data)
                    continue
                tree.add_node(node_from_dict(data))
        return tree

    async def list_sessions(self) -> list[SessionMeta]:
        sessions: list[SessionMeta] = []
        for file_path in sorted(self.base_path.glob("*.jsonl")):
            with file_path.open("r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
                if not lines:
                    continue

                def is_not_meta(line: str) -> bool:
                    return json.loads(line).get("type") != "session_meta"

                first_node = next(
                    (json.loads(line) for line in lines if is_not_meta(line)), None
                )
                if first_node is None:
                    continue
                created_at = datetime.datetime.fromisoformat(first_node["created_at"])
                message_count = sum(1 for line in lines if is_not_meta(line))

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

    async def save_meta(self, session_id: str, meta: dict) -> None:
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
