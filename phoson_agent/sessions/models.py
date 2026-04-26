import uuid
import datetime
from abc import ABC, abstractmethod
from typing import Any
from dataclasses import field, dataclass

from phoson_llm.schemas import Message


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class ConversationNode:
    id: str
    parent_id: str | None
    message: Message
    created_at: datetime.datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMeta:
    id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    message_count: int


@dataclass
class ConversationTree:
    session_id: str
    nodes: dict[str, ConversationNode] = field(default_factory=dict)
    _children: dict[str | None, list[str]] = field(default_factory=dict, init=False)

    @classmethod
    def new(cls, session_id: str | None = None) -> "ConversationTree":
        return cls(session_id=session_id or _new_id())

    def add_node(self, node: ConversationNode) -> None:
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id} already exists.")
        if node.parent_id is not None and node.parent_id not in self.nodes:
            raise ValueError(f"Parent node {node.parent_id} does not exist.")

        self.nodes[node.id] = node
        self._children.setdefault(node.parent_id, []).append(node.id)
        self._children.setdefault(node.id, [])

    def get_path(self, node_id: str | None) -> list[Message]:
        if node_id is None:
            return []
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist.")

        path: list[Message] = []
        cursor: str | None = node_id
        while cursor is not None:
            node = self.nodes[cursor]
            path.append(node.message)
            cursor = node.parent_id
        path.reverse()
        return path

    def append(
        self,
        parent_id: str | None,
        message: Message,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationNode:
        if parent_id is not None and parent_id not in self.nodes:
            raise ValueError(f"Parent node {parent_id} does not exist.")

        node_id = _new_id()
        while node_id in self.nodes:
            node_id = _new_id()

        node = ConversationNode(
            id=node_id,
            parent_id=parent_id,
            message=message,
            created_at=_utc_now(),
            metadata=dict(metadata or {}),
        )
        self.add_node(node)
        return node

    def append_many(
        self,
        parent_id: str | None,
        messages: list[Message],
    ) -> list[ConversationNode]:
        created: list[ConversationNode] = []
        cursor = parent_id
        for message in messages:
            node = self.append(cursor, message)
            created.append(node)
            cursor = node.id
        return created

    def branch(self, from_node_id: str) -> str:
        if from_node_id not in self.nodes:
            raise ValueError(f"Node {from_node_id} does not exist.")
        return from_node_id

    def get_branches(self, node_id: str) -> list[str]:
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        return list(self._children.get(node_id, []))

    def get_leaves(self) -> list[str]:
        # `_children` also tracks root-parent bucket under key `None`.
        return [
            node_id
            for node_id, children in self._children.items()
            if node_id is not None and not children
        ]

    def get_meta(self) -> SessionMeta:
        nodes = list(self.nodes.values())
        if not nodes:
            now = _utc_now()
            return SessionMeta(
                id=self.session_id,
                created_at=now,
                updated_at=now,
                message_count=0,
            )

        return SessionMeta(
            id=self.session_id,
            created_at=min(node.created_at for node in nodes),
            updated_at=max(node.created_at for node in nodes),
            message_count=len(nodes),
        )

    def label(self, node_id: str, text: str) -> None:
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        self.nodes[node_id].metadata["label"] = text

    def node_count(self) -> int:
        return len(self.nodes)


class SessionStorage(ABC):
    @abstractmethod
    async def save(self, tree: ConversationTree) -> None:
        raise NotImplementedError

    @abstractmethod
    async def load(self, session_id: str) -> ConversationTree:
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(self) -> list[SessionMeta]:
        raise NotImplementedError

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        raise NotImplementedError
