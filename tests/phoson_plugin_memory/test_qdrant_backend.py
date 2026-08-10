"""Integration tests for QdrantBackend against a real Qdrant instance.

Requires the ``qdrant-test`` service from ``docker-compose.test.yml``:

    docker compose -f docker-compose.test.yml up -d qdrant-test
    pytest tests/phoson_plugin_memory -q

Skipped automatically (not failed) when qdrant-client isn't installed or
the service isn't reachable.

Uses a tiny deterministic bag-of-words "embedding" over a fixed vocabulary
instead of a real ML model — no heavy dependency needed to prove the real
Qdrant wiring (collection creation, upsert, filtered similarity search,
delete) works end to end with honest, verifiable ranking.
"""

import os
import uuid

import pytest

try:
    from qdrant_client.http.exceptions import ApiException

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

if QDRANT_AVAILABLE:
    from phoson_plugin_memory.qdrant_backend import QdrantBackend

URL = os.environ.get("PHOSON_TEST_QDRANT_URL", "http://localhost:56333")

pytestmark = pytest.mark.skipif(
    not QDRANT_AVAILABLE,
    reason="qdrant-client not installed (pip install qdrant-client)",
)

_VOCAB = ["cat", "dog", "car", "truck", "pizza", "pasta", "sky", "ocean"]


def _toy_embed(text: str) -> list[float]:
    words = set(text.lower().split())
    return [1.0 if w in words else 0.0 for w in _VOCAB]


@pytest.fixture
async def backend():
    collection_name = f"phoson-test-{uuid.uuid4().hex[:8]}"
    store = QdrantBackend(embed_fn=_toy_embed, url=URL, collection_name=collection_name)
    try:
        client = store._ensure_client()
        await client.get_collections()
    except (OSError, ApiException) as exc:
        pytest.skip(
            f"Qdrant not reachable at {URL}: {exc}. Start it with: "
            "docker compose -f docker-compose.test.yml up -d qdrant-test"
        )

    yield store

    client = store._ensure_client()
    if await client.collection_exists(collection_name):
        await client.delete_collection(collection_name)
    await store.close()


@pytest.mark.asyncio
async def test_upsert_creates_collection_lazily(backend):
    client = backend._ensure_client()
    assert not await client.collection_exists(backend.collection_name)

    await backend.upsert("pet", "I have a cat and a dog")

    assert await client.collection_exists(backend.collection_name)


@pytest.mark.asyncio
async def test_search_ranks_by_similarity(backend):
    await backend.upsert("pet", "I have a cat and a dog")
    await backend.upsert("vehicle", "I drive a car and a truck")
    await backend.upsert("food", "I like pizza and pasta")

    results = await backend.search("my cat is cute", top_k=3)

    assert results[0].key == "pet"
    assert results[0].score > results[1].score
    assert {r.key for r in results} == {"pet", "vehicle", "food"}


@pytest.mark.asyncio
async def test_search_respects_top_k(backend):
    await backend.upsert("pet", "cat dog")
    await backend.upsert("vehicle", "car truck")
    await backend.upsert("food", "pizza pasta")

    results = await backend.search("cat", top_k=1)

    assert len(results) == 1
    assert results[0].key == "pet"


@pytest.mark.asyncio
async def test_search_on_empty_collection_returns_empty_list(backend):
    results = await backend.search("anything")
    assert results == []


@pytest.mark.asyncio
async def test_upsert_preserves_metadata(backend):
    await backend.upsert("pet", "cat dog", metadata={"source": "test"})

    results = await backend.search("cat")

    assert results[0].metadata == {"source": "test"}


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_key(backend):
    await backend.upsert("fact", "cat dog")
    await backend.upsert("fact", "car truck")

    # Same key -> same point ID -> exactly one point, holding the latest text.
    # (Qdrant always returns its nearest-K regardless of score, so checking
    # "cat" isn't returned would be wrong — with only one point in the
    # collection it comes back even at score 0. What overwrite actually
    # guarantees is there's no leftover second point under the old text.)
    results = await backend.search("car truck", top_k=10)
    assert len(results) == 1
    assert results[0].key == "fact"
    assert results[0].text == "car truck"
    assert results[0].score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_delete_removes_entry(backend):
    await backend.upsert("pet", "cat dog")

    await backend.delete("pet")

    results = await backend.search("cat")
    assert results == []


@pytest.mark.asyncio
async def test_delete_nonexistent_does_not_raise(backend):
    await backend.delete("never-existed")


@pytest.mark.asyncio
async def test_namespaces_isolate_entries(backend):
    other = QdrantBackend(
        embed_fn=_toy_embed,
        url=URL,
        collection_name=backend.collection_name,
        namespace="other-namespace",
    )
    await backend.upsert("pet", "cat dog")
    await other.upsert("vehicle", "car truck")

    # Both searches query for "cat", which is only relevant to "phoson"'s
    # entry. Qdrant always returns its namespace's nearest-K regardless of
    # score, so if the filter leaked, "other" would still show "pet" (the
    # more similar entry, from the wrong namespace) instead of its own,
    # irrelevant "vehicle".
    own_results = await backend.search("cat")
    other_results = await other.search("cat")

    assert [r.key for r in own_results] == ["pet"]
    assert [r.key for r in other_results] == ["vehicle"]
    await other.close()
