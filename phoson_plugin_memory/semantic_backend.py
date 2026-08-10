"""Interface for semantic (similarity-search) memory backends.

Deliberately NOT MemoryBackend: that interface is exact-key lookup
(get/set/delete/list_keys), which doesn't fit similarity search at all —
there's no "get by key" here, only "upsert this text under a key" and
"find the entries most similar to this query".
"""

from abc import ABC, abstractmethod
from typing import Any
from dataclasses import field, dataclass


@dataclass
class SemanticMatch:
    """A single similarity-search hit.

    Args:
        key: The key the matched entry was stored under.
        text: The original text that was embedded.
        score: Similarity score (backend-specific scale; higher is more similar).
        metadata: Arbitrary metadata stored alongside the entry.
    """

    key: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class SemanticMemoryBackend(ABC):
    """Abstract backend for semantic (embedding-based) memory."""

    @abstractmethod
    async def upsert(
        self, key: str, text: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Embed ``text`` and store it under ``key`` (overwriting any prior value)."""
        raise NotImplementedError

    @abstractmethod
    async def search(self, query: str, top_k: int = 5) -> list[SemanticMatch]:
        """Return up to ``top_k`` entries most similar to ``query``, ranked by score."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the entry stored under ``key``. Must not raise if absent."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release any underlying connections. Safe to call multiple times."""
        raise NotImplementedError
