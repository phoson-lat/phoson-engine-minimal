"""Tool-call detail modes and persisted tool timings (UI-free).

``/details`` cycles the transcript's tool calls through three levels:

- ``full``: one card per call with its specialized body (diff, write
  summary, bash output);
- ``headers``: one card per call, header + ✓/✗ · duration only;
- ``summary``: every *run* of consecutive tool calls (a "tramo" between two
  pieces of assistant text) folds into a single ``N tools used`` line.

Tool durations are not part of the LLM message schema, so the controller
stores them on the tree node that carries the matching ``ToolResultBlock``
(``node.metadata["tool_durations"]``) and replays read them back from there.
"""

from typing import TYPE_CHECKING, Final
from collections.abc import Mapping, Iterable

if TYPE_CHECKING:
    from phoson_agent.sessions.models import ConversationNode

TOOL_DETAIL_MODES: Final[tuple[str, ...]] = ("full", "headers", "summary")
#: Long sessions read best folded; ``/details`` expands them on demand.
DEFAULT_TOOL_DETAIL: Final[str] = "summary"

#: ``node.metadata`` key holding ``{tool_call_id: duration_ms}``.
TOOL_DURATIONS_KEY: Final[str] = "tool_durations"


def is_valid_tool_detail(mode: str) -> bool:
    return mode in TOOL_DETAIL_MODES


def next_tool_detail(mode: str) -> str:
    """The mode a bare ``/details`` switches to (full → headers → summary)."""
    if mode not in TOOL_DETAIL_MODES:
        return DEFAULT_TOOL_DETAIL
    index = TOOL_DETAIL_MODES.index(mode)
    return TOOL_DETAIL_MODES[(index + 1) % len(TOOL_DETAIL_MODES)]


def stamp_tool_durations(
    nodes: Iterable["ConversationNode"], durations: Mapping[str, int]
) -> None:
    """Record each tool result's duration on the node that carries it."""
    if not durations:
        return
    from phoson_llm.schemas import ToolResultBlock

    for node in nodes:
        content = node.message.content
        if isinstance(content, str):
            continue
        found = {
            block.tool_call_id: durations[block.tool_call_id]
            for block in content
            if isinstance(block, ToolResultBlock) and block.tool_call_id in durations
        }
        if found:
            node.metadata.setdefault(TOOL_DURATIONS_KEY, {}).update(found)


def tool_durations_from_nodes(nodes: Iterable["ConversationNode"]) -> dict[str, int]:
    """Merge the persisted tool durations of *nodes* (replay input)."""
    merged: dict[str, int] = {}
    for node in nodes:
        stored = node.metadata.get(TOOL_DURATIONS_KEY)
        if isinstance(stored, dict):
            merged.update(
                {
                    str(call_id): int(ms)
                    for call_id, ms in stored.items()
                    if isinstance(ms, int | float)
                }
            )
    return merged


__all__ = [
    "DEFAULT_TOOL_DETAIL",
    "TOOL_DETAIL_MODES",
    "TOOL_DURATIONS_KEY",
    "is_valid_tool_detail",
    "next_tool_detail",
    "stamp_tool_durations",
    "tool_durations_from_nodes",
]
