"""Unit tests for SemanticMemoryPlugin's tool contract, decoupled from Qdrant.

Exercises memory_remember/memory_recall against a trivial in-process
SemanticMemoryBackend, so these tests don't need a running Qdrant.
"""

import pytest

from phoson_plugin_memory.semantic_plugin import SemanticMemoryPlugin
from phoson_plugin_memory.semantic_backend import SemanticMatch, SemanticMemoryBackend


class FakeSemanticBackend(SemanticMemoryBackend):
    """Minimal in-process SemanticMemoryBackend for exercising the plugin/tool
    contract without a real Qdrant connection or embedder."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, dict]] = {}

    async def upsert(self, key, text, metadata=None) -> None:
        self._store[key] = (text, metadata or {})

    async def search(self, query, top_k=5) -> list[SemanticMatch]:
        matches = [
            SemanticMatch(key=k, text=t, score=1.0, metadata=m)
            for k, (t, m) in self._store.items()
            if query.lower() in t.lower()
        ]
        return matches[:top_k]

    async def delete(self, key) -> None:
        self._store.pop(key, None)

    async def close(self) -> None:
        pass


def _noop_embed(text: str) -> list[float]:
    return [0.0]


@pytest.fixture
def plugin() -> SemanticMemoryPlugin:
    p = SemanticMemoryPlugin(embed_fn=_noop_embed)
    p.backend = FakeSemanticBackend()
    return p


def test_plugin_properties():
    plugin = SemanticMemoryPlugin(embed_fn=_noop_embed)
    assert plugin.name == "phoson-plugin-memory-semantic"
    assert plugin.version == "0.1.0"
    assert "semantic" in plugin.description.lower()


def test_initialize_without_embed_fn_raises():
    plugin = SemanticMemoryPlugin()
    with pytest.raises(ValueError, match="embed_fn"):
        plugin.initialize()


def test_constructor_embed_fn_survives_empty_configure():
    plugin = SemanticMemoryPlugin(embed_fn=_noop_embed)
    plugin.configure({})  # registry always calls configure(), even for instances
    plugin.initialize()

    assert plugin.backend is not None


def test_configure_can_override_embed_fn_and_url():
    other_embed = lambda t: [1.0]  # noqa: E731

    plugin = SemanticMemoryPlugin(embed_fn=_noop_embed)
    plugin.configure({"embed_fn": other_embed, "url": "http://qdrant.example:6333"})
    plugin.initialize()

    assert plugin.backend.embed_fn is other_embed
    assert plugin.backend.url == "http://qdrant.example:6333"


def test_get_tools_returns_remember_and_recall():
    plugin = SemanticMemoryPlugin(embed_fn=_noop_embed)
    plugin.backend = FakeSemanticBackend()

    tools = plugin.get_tools()
    names = {t.name for t in tools}

    assert names == {"memory_remember", "memory_recall"}


@pytest.mark.asyncio
async def test_remember_then_recall_roundtrip(plugin):
    remember_tool = next(t for t in plugin.get_tools() if t.name == "memory_remember")
    recall_tool = next(t for t in plugin.get_tools() if t.name == "memory_recall")

    result = await remember_tool.handler({"key": "fact-1", "text": "the sky is blue"})
    assert result == {"stored": True, "key": "fact-1"}

    recalled = await recall_tool.handler({"query": "sky"})
    assert recalled["matches"] == [
        {"key": "fact-1", "text": "the sky is blue", "score": 1.0, "metadata": {}}
    ]


@pytest.mark.asyncio
async def test_recall_with_no_matches_returns_empty_list(plugin):
    recall_tool = next(t for t in plugin.get_tools() if t.name == "memory_recall")

    recalled = await recall_tool.handler({"query": "nothing stored yet"})

    assert recalled == {"matches": []}


@pytest.mark.asyncio
async def test_remember_missing_text_returns_error(plugin):
    remember_tool = next(t for t in plugin.get_tools() if t.name == "memory_remember")

    result = await remember_tool.handler({"key": "fact-1"})

    assert "error" in result


@pytest.mark.asyncio
async def test_recall_missing_query_returns_error(plugin):
    recall_tool = next(t for t in plugin.get_tools() if t.name == "memory_recall")

    result = await recall_tool.handler({})

    assert "error" in result


def test_get_tools_before_initialize_raises():
    plugin = SemanticMemoryPlugin(embed_fn=_noop_embed)
    with pytest.raises(AssertionError):
        plugin.get_tools()


def test_cleanup_clears_backend(plugin):
    plugin.cleanup()
    assert plugin.backend is None
