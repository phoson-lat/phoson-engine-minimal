"""Qdrant-backed SemanticMemoryBackend implementation.

The embedding step (text -> vector) is not implemented here. Callers inject
``embed_fn`` — a plain ``str -> list[float]`` callable, sync or async — so
this package doesn't force any embedding provider/model and stays free of
heavy dependencies (no torch, no bundled API client for a specific LLM
provider).

Qdrant point IDs must be an unsigned int or a UUID (verified against a real
instance — plain arbitrary strings are rejected with a 400). Since our keys
are arbitrary strings, they're mapped to a deterministic UUID5 and the
original key is kept in the payload for result identification and delete.
"""

import uuid
import asyncio
import inspect
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

try:
    from qdrant_client import AsyncQdrantClient, models

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

from phoson_plugin_memory.semantic_backend import SemanticMatch, SemanticMemoryBackend

EmbedFn = Callable[[str], "list[float] | Awaitable[list[float]]"]

_ID_NAMESPACE = uuid.UUID("6f6d6570-6873-6f70-2d6d-656d6f727921")  # arbitrary, fixed


@dataclass
class QdrantBackend(SemanticMemoryBackend):
    """Semantic memory tier backed by Qdrant.

    Args:
        embed_fn: ``str -> list[float]`` (sync or async). Required — no
            default embedder is provided.
        url: Qdrant connection URL (default ``http://localhost:6333``).
        collection_name: Qdrant collection to store entries in. Created
            lazily on first ``upsert()``, sized to match the first
            embedding's dimensionality.
        namespace: Logical scope for keys, so multiple agents/sessions can
            share one collection without colliding. Enforced via a payload
            filter on both search and delete.
    """

    embed_fn: EmbedFn
    url: str = "http://localhost:6333"
    collection_name: str = "phoson_memory"
    namespace: str = "phoson"

    _client: Any = field(default=None, init=False, repr=False)
    _collection_ready: bool = field(default=False, init=False, repr=False)
    _collection_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    def _ensure_client(self) -> Any:
        if not QDRANT_AVAILABLE:
            raise ImportError(
                "qdrant-client not installed. Install with: pip install qdrant-client "
                "or pip install 'phoson-engine-minimal[memory]'"
            )
        if self._client is None:
            self._client = AsyncQdrantClient(url=self.url, check_compatibility=False)
        return self._client

    async def _embed(self, text: str) -> list[float]:
        result = self.embed_fn(text)
        if inspect.isawaitable(result):
            result = await result
        return list(result)

    def _point_id(self, key: str) -> str:
        return str(uuid.uuid5(_ID_NAMESPACE, f"{self.namespace}:{key}"))

    def _namespace_filter(self) -> Any:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="namespace", match=models.MatchValue(value=self.namespace)
                )
            ]
        )

    async def _ensure_collection(self, vector_size: int) -> None:
        if self._collection_ready:
            return
        client = self._ensure_client()
        async with self._collection_lock:
            if self._collection_ready:
                return
            if not await client.collection_exists(self.collection_name):
                await client.create_collection(
                    self.collection_name,
                    vectors_config=models.VectorParams(
                        size=vector_size, distance=models.Distance.COSINE
                    ),
                )
            self._collection_ready = True

    async def upsert(
        self, key: str, text: str, metadata: dict[str, Any] | None = None
    ) -> None:
        vector = await self._embed(text)
        await self._ensure_collection(len(vector))
        client = self._ensure_client()
        await client.upsert(
            self.collection_name,
            points=[
                models.PointStruct(
                    id=self._point_id(key),
                    vector=vector,
                    payload={
                        "namespace": self.namespace,
                        "key": key,
                        "text": text,
                        "metadata": metadata or {},
                    },
                )
            ],
        )

    async def search(self, query: str, top_k: int = 5) -> list[SemanticMatch]:
        client = self._ensure_client()
        if not await client.collection_exists(self.collection_name):
            return []
        vector = await self._embed(query)
        response = await client.query_points(
            self.collection_name,
            query=vector,
            limit=top_k,
            query_filter=self._namespace_filter(),
            with_payload=True,
        )
        return [
            SemanticMatch(
                key=point.payload["key"],
                text=point.payload["text"],
                score=point.score,
                metadata=point.payload.get("metadata") or {},
            )
            for point in response.points
        ]

    async def delete(self, key: str) -> None:
        client = self._ensure_client()
        if not await client.collection_exists(self.collection_name):
            return
        await client.delete(self.collection_name, points_selector=[self._point_id(key)])

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
