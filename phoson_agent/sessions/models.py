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
    """A single node in a conversation tree.

    Represents a message in a branchable conversation history.

    Args:
        id: Unique node identifier.
        parent_id: ID of the parent node, or None for root nodes.
        message: The message content.
        created_at: Timestamp when the node was created.
        metadata: Optional metadata dictionary (e.g., labels).
    """

    id: str
    parent_id: str | None
    message: Message
    created_at: datetime.datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMeta:
    """Metadata for a conversation session.

    Tracks session-level statistics and identification.

    Args:
        id: Unique session identifier.
        created_at: When the session was created.
        updated_at: When the session was last modified.
        message_count: Total number of messages in the session.
        total_cost: Accumulated cost in USD.
        total_tokens: Total tokens consumed.
        step_count: Number of agent steps executed.
        last_model: Most recently used model.
    """

    id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    message_count: int
    total_cost: float = 0.0
    total_tokens: int = 0
    # F-34: the input/output split (persisted alongside ``total_tokens``) so a
    # resumed session can show real in/out figures instead of dumping the sum
    # into output. Pre-F-34 files only carry ``total_tokens``; the fields
    # default to 0 and the CLI back-fills output from the sum on resume.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    step_count: int = 0
    last_model: str | None = None
    title: str | None = None


@dataclass
class ConversationTree:
    """A branchable conversation tree for non-linear session history.

    Unlike linear chat histories, this tree allows branching where you can
    explore different conversation paths and return to previous branches.

    Args:
        session_id: Unique identifier for this session.
        nodes: Dictionary mapping node IDs to ConversationNode objects.
        total_cost: Accumulated cost in USD.
        total_tokens: Total tokens consumed.
        step_count: Number of agent steps executed.
        last_model: Most recently used model.
    """

    session_id: str
    nodes: dict[str, ConversationNode] = field(default_factory=dict)
    _children: dict[str | None, list[str]] = field(default_factory=dict, init=False)

    total_cost: float = 0.0
    total_tokens: int = 0
    # F-34: persisted split (see SessionMeta); back-filled on resume when
    # absent so a legacy file's sum lands in output, not a misleading 0.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    step_count: int = 0
    last_model: str | None = None
    title: str | None = None

    @classmethod
    def new(cls, session_id: str | None = None) -> "ConversationTree":
        """Create a new conversation tree with an optional session ID.

        Args:
            session_id: Optional session ID. If None, a random ID is generated.

        Returns:
            New ConversationTree instance.
        """
        return cls(session_id=session_id or _new_id())

    def add_node(self, node: ConversationNode) -> None:
        """Add a node to the tree.

        Args:
            node: The ConversationNode to add.

        Raises:
            ValueError: If node ID already exists or parent doesn't exist.
        """
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id} already exists.")
        if node.parent_id is not None and node.parent_id not in self.nodes:
            raise ValueError(f"Parent node {node.parent_id} does not exist.")

        self.nodes[node.id] = node
        self._children.setdefault(node.parent_id, []).append(node.id)
        self._children.setdefault(node.id, [])

    def get_path(self, node_id: str | None) -> list[Message]:
        """Get the message path from root to the specified node.

        Args:
            node_id: Target node ID, or None to return empty list.

        Returns:
            List of messages from root to the target node (inclusive).

        Raises:
            ValueError: If node_id doesn't exist in the tree.
        """
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
        """Append a new message node to the tree.

        Args:
            parent_id: Parent node ID, or None to create a root node.
            message: The message to add.
            metadata: Optional metadata dictionary.

        Returns:
            The newly created ConversationNode.

        Raises:
            ValueError: If parent_id doesn't exist.
        """
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
        """Append multiple messages as consecutive nodes.

        Args:
            parent_id: Parent node ID for the first message, or None for root.
            messages: List of messages to add sequentially.

        Returns:
            List of created ConversationNodes in order.
        """
        created: list[ConversationNode] = []
        cursor = parent_id
        for message in messages:
            node = self.append(cursor, message)
            created.append(node)
            cursor = node.id
        return created

    def branch(self, from_node_id: str) -> str:
        """Start a new branch from an existing node.

        Args:
            from_node_id: The node to branch from.

        Returns:
            The node_id to use as parent for the new branch.

        Raises:
            ValueError: If from_node_id doesn't exist.
        """
        if from_node_id not in self.nodes:
            raise ValueError(f"Node {from_node_id} does not exist.")
        return from_node_id

    def get_branches(self, node_id: str) -> list[str]:
        """Get child node IDs from a node.

        Args:
            node_id: The parent node ID.

        Returns:
            List of child node IDs.

        Raises:
            ValueError: If node_id doesn't exist.
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        return list(self._children.get(node_id, []))

    def get_leaves(self) -> list[str]:
        """Get all leaf node IDs (nodes with no children).

        Returns:
            List of leaf node IDs.
        """
        return [
            node_id
            for node_id, children in self._children.items()
            if node_id is not None and not children
        ]

    def get_meta(self) -> SessionMeta:
        """Get session metadata computed from the tree.

        Returns:
            SessionMeta with computed creation time, message count, and totals.
        """
        nodes = list(self.nodes.values())
        if not nodes:
            now = _utc_now()
            return SessionMeta(
                id=self.session_id,
                created_at=now,
                updated_at=now,
                message_count=0,
                total_cost=self.total_cost,
                total_tokens=self.total_tokens,
                total_input_tokens=self.total_input_tokens,
                total_output_tokens=self.total_output_tokens,
                step_count=self.step_count,
                last_model=self.last_model,
                title=self.title,
            )

        return SessionMeta(
            id=self.session_id,
            created_at=min(node.created_at for node in nodes),
            updated_at=max(node.created_at for node in nodes),
            message_count=len(nodes),
            total_cost=self.total_cost,
            total_tokens=self.total_tokens,
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            step_count=self.step_count,
            last_model=self.last_model,
            title=self.title,
        )

    def label(self, node_id: str, text: str) -> None:
        """Add or update a label on a node.

        Args:
            node_id: The node ID to label.
            text: The label text.

        Raises:
            ValueError: If node_id doesn't exist.
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        self.nodes[node_id].metadata["label"] = text

    def node_count(self) -> int:
        """Get the total number of nodes in the tree.

        Returns:
            Number of nodes.
        """
        return len(self.nodes)

    def update_session_meta(
        self,
        total_cost: float | None = None,
        total_tokens: int | None = None,
        total_input_tokens: int | None = None,
        total_output_tokens: int | None = None,
        step_count: int | None = None,
        last_model: str | None = None,
        title: str | None = None,
    ) -> None:
        """Update session-level metadata."""
        if total_cost is not None:
            self.total_cost = total_cost
        if total_tokens is not None:
            self.total_tokens = total_tokens
        if total_input_tokens is not None:
            self.total_input_tokens = total_input_tokens
        if total_output_tokens is not None:
            self.total_output_tokens = total_output_tokens
        if step_count is not None:
            self.step_count = step_count
        if last_model is not None:
            self.last_model = last_model
        if title is not None:
            self.title = title


class SessionStorage(ABC):
    """Abstract base class for session storage backends.

    Implement this interface to provide custom persistence for conversation trees.

    Example:
        class S3Storage(SessionStorage):
            async def save(self, tree: ConversationTree) -> None:
                ...

            async def load(self, session_id: str) -> ConversationTree:
                ...

            async def list_sessions(self) -> list[SessionMeta]:
                ...

            async def delete(self, session_id: str) -> None:
                ...
    """

    @abstractmethod
    async def save(self, tree: ConversationTree) -> None:
        """Persist a conversation tree.

        Args:
            tree: The ConversationTree to save.
        """
        raise NotImplementedError

    @abstractmethod
    async def load(self, session_id: str) -> ConversationTree:
        """Load a conversation tree by session ID.

        Args:
            session_id: The session identifier.

        Returns:
            The ConversationTree for this session.

        Raises:
            FileNotFoundError: If session doesn't exist.
        """
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(self) -> list[SessionMeta]:
        """List all available sessions.

        Returns:
            List of SessionMeta for each session.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Delete a session by ID.

        Args:
            session_id: The session identifier to delete.

        Raises:
            FileNotFoundError: If session doesn't exist.
        """
        raise NotImplementedError
