"""Semantic memory plugin: exposes QdrantBackend as memory_remember/memory_recall.

Kept separate from MemoryPlugin — the tool contract is fundamentally
different (store-and-rank-by-similarity vs. exact-key get/set), not just a
different storage backend behind the same tools.
"""

from typing import Any

from phoson_agent import Plugin, AgentTool

from .qdrant_backend import EmbedFn, QdrantBackend
from .semantic_backend import SemanticMemoryBackend


class SemanticMemoryPlugin(Plugin):
    """Plugin providing similarity-search memory tools backed by Qdrant.

    ``embed_fn`` has no default and isn't JSON-config-friendly (it's a
    Python callable), so pass it either to the constructor or via
    ``config["embed_fn"]`` when the plugin is built from a Python spec:

        plugins=[SemanticMemoryPlugin(embed_fn=my_embed_fn)]

        # or

        plugins=[{
            "name": "path:./my_semantic_plugin.py",
            "config": {"embed_fn": my_embed_fn, "url": "http://localhost:6333"},
        }]

    Configuration:
        embed_fn: ``str -> list[float]`` (sync or async). Required.
        url: Qdrant connection URL (default ``http://localhost:6333``).
        collection_name: Qdrant collection name (default ``"phoson_memory"``).
        namespace: Logical scope for keys (default ``"phoson"``).
        tool_prefix: Prepended to every tool name (default ``""``). Set
            this if you add more than one ``SemanticMemoryPlugin`` to the
            same agent (e.g. two different collections), to avoid both
            registering identically-named tools.
    """

    def __init__(
        self,
        *,
        embed_fn: EmbedFn | None = None,
        url: str = "http://localhost:6333",
        collection_name: str = "phoson_memory",
        namespace: str = "phoson",
        tool_prefix: str = "",
    ) -> None:
        self._embed_fn = embed_fn
        self._url = url
        self._collection_name = collection_name
        self._namespace = namespace
        self._tool_prefix = tool_prefix
        self.backend: SemanticMemoryBackend | None = None

    @property
    def name(self) -> str:
        return "phoson-plugin-memory-semantic"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return (
            "Semantic (similarity search) memory tools for Phoson Agent, "
            "backed by Qdrant"
        )

    def configure(self, config: dict[str, Any]) -> None:
        if "embed_fn" in config:
            self._embed_fn = config["embed_fn"]
        self._url = config.get("url", self._url)
        self._collection_name = config.get("collection_name", self._collection_name)
        self._namespace = config.get("namespace", self._namespace)
        self._tool_prefix = config.get("tool_prefix", self._tool_prefix)

    def initialize(self) -> None:
        if not callable(self._embed_fn):
            raise ValueError(
                "phoson-plugin-memory-semantic requires 'embed_fn' (a callable "
                "str -> vector), passed to the constructor or via config['embed_fn']"
            )
        self.backend = QdrantBackend(
            embed_fn=self._embed_fn,
            url=self._url,
            collection_name=self._collection_name,
            namespace=self._namespace,
        )

    def _tool_name(self, base: str) -> str:
        return f"{self._tool_prefix}{base}"

    def get_tools(self) -> list[AgentTool]:
        assert self.backend is not None, "initialize() must run before get_tools()"
        backend = self.backend

        async def memory_remember(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            key = args.get("key")
            text = args.get("text")
            if not key or not text:
                return {"error": "Missing required field(s): key, text"}
            await backend.upsert(key, text, metadata=args.get("metadata"))
            return {"stored": True, "key": key}

        async def memory_recall(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            query = args.get("query")
            if not query:
                return {"error": "Missing required field: query"}
            top_k = int(args.get("top_k", 5))
            matches = await backend.search(query, top_k=top_k)
            return {
                "matches": [
                    {
                        "key": m.key,
                        "text": m.text,
                        "score": m.score,
                        "metadata": m.metadata,
                    }
                    for m in matches
                ]
            }

        async def memory_forget(
            args: dict[str, Any], _context: Any | None = None
        ) -> dict[str, Any]:
            key = args.get("key")
            if not key:
                return {"error": "Missing required field: key"}
            await backend.delete(key)
            return {"forgotten": True, "key": key}

        return [
            AgentTool(
                name=self._tool_name("memory_remember"),
                description=(
                    "Store a piece of text in semantic memory under a key, so it "
                    "can later be found by meaning via memory_recall (not just by "
                    "exact key)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Memory key"},
                        "text": {
                            "type": "string",
                            "description": "Text to store and make searchable",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Optional metadata to store with the text",
                        },
                    },
                    "required": ["key", "text"],
                },
                handler=memory_remember,
            ),
            AgentTool(
                name=self._tool_name("memory_recall"),
                description=(
                    "Find entries in semantic memory whose meaning is most similar "
                    "to a query, ranked by similarity score."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text to semantically search memories for",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Max results to return (default 5)",
                        },
                    },
                    "required": ["query"],
                },
                handler=memory_recall,
            ),
            AgentTool(
                name=self._tool_name("memory_forget"),
                description="Remove a piece of text from semantic memory by key.",
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Memory key to remove",
                        },
                    },
                    "required": ["key"],
                },
                handler=memory_forget,
            ),
        ]

    def cleanup(self) -> None:
        # backend.close() is async (qdrant AsyncQdrantClient); callers needing
        # a clean shutdown should `await plugin.aclose()` directly instead.
        self.backend = None

    async def aclose(self) -> None:
        """Async, awaitable teardown: closes the underlying Qdrant client."""
        if self.backend is not None:
            await self.backend.close()
        self.backend = None


def create_plugin() -> SemanticMemoryPlugin:
    """Factory function. Note: embed_fn still needs to be set via config."""
    return SemanticMemoryPlugin()
