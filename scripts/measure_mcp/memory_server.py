"""Hermetic stdio MCP server mimicking @modelcontextprotocol/server-memory.

Used by ``scripts/measure_tool_definitions.py`` (issue #148). The memory
server is the second typical MCP flavor: few tools, but with *rich*
structured schemas (arrays of typed objects), which is what makes MCP
catalogs expensive in tokens.
"""

from typing import TypedDict

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("memory")


class Entity(TypedDict):
    name: str
    entity_type: str
    observations: list[str]


class Relation(TypedDict):
    source: str
    target: str
    relation_type: str


class ObservationAdd(TypedDict):
    entity_name: str
    contents: list[str]


class ObservationDelete(TypedDict):
    entity_name: str
    contents: list[str]


class _MemoryStore:
    entities: list[Entity] = []
    relations: list[Relation] = []


_store = _MemoryStore()


@mcp.tool()
def create_entities(entities: list[Entity]) -> str:
    """Create multiple new entities in the knowledge graph.

    Each entity must have: name (unique), entity_type, and observations
    (a list of strings that are facts about the entity).
    """
    _store.entities.extend(entities)
    return f"Created {len(entities)} entities"


@mcp.tool()
def create_relations(relations: list[Relation]) -> str:
    """Create multiple new relations between entities in the knowledge graph.

    Each relation must have: source (existing entity name), target
    (existing entity name), and relation_type describing how they connect.
    """
    _store.relations.extend(relations)
    return f"Created {len(relations)} relations"


@mcp.tool()
def add_observations(observations: list[ObservationAdd]) -> str:
    """Add new observations to existing entities in the knowledge graph.

    Each item must have: entity_name (an existing entity) and contents
    (a list of new factual observations to append).
    """
    for obs in observations:
        for e in _store.entities:
            if e["name"] == obs["entity_name"]:
                e["observations"].extend(obs["contents"])
    return f"Added observations for {len(observations)} entities"


@mcp.tool()
def search_nodes(query: str) -> str:
    """Search for nodes in the knowledge graph based on a query string.

    Returns entities and relations whose name, type or observations
    match the query.
    """
    hits = [
        f"{e['name']} ({e['entity_type']}): " + "; ".join(e["observations"])
        for e in _store.entities
        if query.lower() in e["name"].lower()
        or any(query.lower() in o.lower() for o in e["observations"])
    ]
    return "\n".join(hits) or "No matching nodes"


@mcp.tool()
def open_nodes(names: list[str]) -> str:
    """Open specific nodes in the knowledge graph by their names.

    Returns full details (entity type, all observations) for each named
    entity, plus every relation it participates in.
    """
    lines = []
    for name in names:
        for e in _store.entities:
            if e["name"] == name:
                lines.append(f"{e['name']} [{e['entity_type']}]:")
                lines.extend(f"  - {o}" for o in e["observations"])
    for r in _store.relations:
        if r["source"] in names or r["target"] in names:
            lines.append(f"{r['source']} --{r['relation_type']}--> {r['target']}")
    return "\n".join(lines) or "No nodes found"


@mcp.tool()
def delete_entities(entity_names: list[str]) -> str:
    """Delete multiple entities and their associated relations from the graph."""
    before = len(_store.entities)
    _store.entities = [e for e in _store.entities if e["name"] not in entity_names]
    _store.relations = [
        r
        for r in _store.relations
        if r["source"] not in entity_names and r["target"] not in entity_names
    ]
    return f"Deleted {before - len(_store.entities)} entities"


@mcp.tool()
def delete_observations(deletions: list[ObservationDelete]) -> str:
    """Delete specific observations from entities in the knowledge graph.

    Each item must have: entity_name and the exact contents to remove.
    """
    removed = 0
    for d in deletions:
        for e in _store.entities:
            if e["name"] == d["entity_name"]:
                for c in d["contents"]:
                    if c in e["observations"]:
                        e["observations"].remove(c)
                        removed += 1
    return f"Deleted {removed} observations"


@mcp.tool()
def delete_relations(relations: list[Relation]) -> str:
    """Delete multiple relations from the knowledge graph.

    Each relation to delete is identified by its source, target and
    relation_type (all three must match exactly).
    """
    removed = 0
    for r in relations:
        if r in _store.relations:
            _store.relations.remove(r)
            removed += 1
    return f"Deleted {removed} relations"


if __name__ == "__main__":
    mcp.run()
