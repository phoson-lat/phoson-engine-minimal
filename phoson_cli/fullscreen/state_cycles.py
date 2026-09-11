"""Runtime state-toggling commands for the full-screen front end (#187).

Extracted from ``app.py``:
- ``toggle_reasoning`` (Ctrl+T): live reasoning toggle or transcript expansion
- ``cycle_permission_mode`` (Shift+Tab): ask ↔ auto policy toggle for bash
- ``cycle_reasoning_effort`` (Ctrl+E): cycle reasoning effort levels
"""

import time
from typing import Any

from phoson_llm.schemas import REASONING_EFFORTS

from ..config import save_config


def toggle_reasoning(app: Any) -> None:
    """Ctrl+T: toggle the live thinking block, or expand a past node's.

    While streaming, toggles the in-progress reasoning panel. Once
    idle, expands the reasoning of the newest node on the current
    path that has any — a node's reasoning is shown at most once per
    session (the transcript is append-only).
    """
    if app.sink.current_turn is not None:
        new_state = app.sink.toggle_live_reasoning()
        if getattr(app.repl.config, "show_reasoning", True) != new_state:
            app.repl.config.show_reasoning = new_state
            save_config(app.repl.config, only_fields={"show_reasoning"})
            app.sink.show_reasoning_default = new_state
        return

    cursor: str | None = app.repl.current_node_id
    path_ids: list[str] = []
    while cursor is not None:
        path_ids.append(cursor)
        node = app.repl.tree.nodes.get(cursor)
        cursor = node.parent_id if node is not None else None
    path_ids.reverse()

    for node_id in path_ids:
        node = app.repl.tree.nodes.get(node_id)
        reasoning = node.metadata.get("reasoning") if node else None
        if not reasoning:
            continue
        if node_id in app.repl._expanded_reasoning:
            app.sink.notify(
                "info",
                "Reasoning already expanded (the transcript is append-only).",
            )
            return
        app.repl._expanded_reasoning.add(node_id)
        app.sink.expand_reasoning(str(reasoning))
        return


def cycle_permission_mode(app: Any) -> None:
    """Shift+Tab (T-6): cycle the visible permission mode ask → auto.

    The mode is the durable per-tool policy (``permissions.json``);
    cycling it sets *bash*'s level, which is the tool the SOTA
    harnesses gate by default. The header chip refreshes immediately
    and the user is told the new state + how to fine-tune
    per-tool with /permissions.
    """
    from ..permissions_store import LEVEL_ASK, set_level, load_policy, save_policy

    policy = load_policy()
    current = policy.levels.get("bash")
    if current == LEVEL_ASK:
        set_level(policy, "bash", "allow")
        new_mode = "auto"
    else:
        set_level(policy, "bash", LEVEL_ASK)
        new_mode = "ask"
    save_policy(policy)
    app._perm_mode_cached = new_mode
    app._perm_mode_checked_at = time.monotonic()
    app._header_cache_key = None  # rebuild the chip on the next frame
    app.sink.notify(
        "info",
        f"Permission mode → {new_mode}"
        + (
            " — bash commands now confirm with Yes / Always / No"
            if new_mode == "ask"
            else " — bash runs freely (per-tool rules: /permissions)"
        ),
    )


def cycle_reasoning_effort(app: Any) -> None:
    """Ctrl+E: cycle the reasoning effort off → low → medium → high →
    xhigh → max (wraps to off).
    """
    current = app.repl.config.reasoning_effort
    if current not in REASONING_EFFORTS:
        current = None  # "off"
    levels = (*REASONING_EFFORTS, None)
    next_effort = levels[(levels.index(current) + 1) % len(levels)]
    app.repl.config.reasoning_effort = next_effort
    save_config(app.repl.config, only_fields={"reasoning_effort"})
    app._header_cache_key = None  # rebuild the chip on the next frame
    app.sink.notify(
        "info",
        f"Reasoning effort → {next_effort or 'off'}"
        " · applies from the next turn (explicit: /reasoning-effort)",
    )


__all__ = [
    "toggle_reasoning",
    "cycle_permission_mode",
    "cycle_reasoning_effort",
]
