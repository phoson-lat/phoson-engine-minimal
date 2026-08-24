"""Pure presentation helpers for the CLI.

These functions used to live as methods on :class:`PhosonRepl` but they
have nothing to do with REPL state — they only need a tree (or just a
console) to do their work. Lifting them out keeps ``repl.py`` focused
on the input loop and makes the rendering trivially testable.
"""

from pathlib import Path

from rich.rule import Rule
from rich.text import Text
from rich.columns import Columns
from rich.console import Group, Console, RenderableType

from phoson_cli.theme import Theme
from phoson_agent.sessions.models import ConversationTree

# Loaded once at import time so the banner prints instantly on cold REPL start.
_PHOS_ART = (
    (Path(__file__).parent / "phos-ascii.txt").read_text(encoding="utf-8").rstrip("\n")
)


# ─── Conversation tree ───────────────────────────────────────────────────────


def render_tree_ascii(
    tree: ConversationTree,
    current_node_id: str | None,
) -> str:
    """Render ``tree`` as an ASCII diagram with the current node highlighted.

    Args:
        tree: The conversation tree to render.
        current_node_id: The id of the node the REPL is currently
            "sitting on" — it is rendered with an open marker and a
            ``← current`` annotation.

    Returns:
        The diagram as a single string (newline-joined). For an empty
        tree the placeholder ``(empty session)`` is returned instead.
    """
    if not tree.nodes:
        return "(empty session)"

    children: dict[str | None, list[str]] = {}
    for node in tree.nodes.values():
        children.setdefault(node.parent_id, []).append(node.id)
        children.setdefault(node.id, [])
    for child_ids in children.values():
        child_ids.sort(key=lambda nid: tree.nodes[nid].created_at)

    def render_node(node_id: str, prefix: str, is_last: bool) -> list[str]:
        node = tree.nodes[node_id]
        marker = "○" if node_id == current_node_id else "●"
        preview = _message_preview(node.message.content)
        tail = "  ← current" if node_id == current_node_id else ""
        branch = "└─ " if is_last else "├─ "
        lines = [f"{prefix}{branch}{marker} {node.id}  {preview}{tail}"]
        next_prefix = prefix + ("   " if is_last else "│  ")
        kids = children.get(node_id, [])
        for i, child_id in enumerate(kids):
            lines.extend(render_node(child_id, next_prefix, i == len(kids) - 1))
        return lines

    roots = children.get(None, [])
    lines: list[str] = []
    for i, root_id in enumerate(roots):
        root = tree.nodes[root_id]
        marker = "○" if root_id == current_node_id else "●"
        preview = _message_preview(root.message.content)
        tail = "  ← current" if root_id == current_node_id else ""
        lines.append(f"{marker} {root.id}  {preview}{tail}")
        kids = children.get(root_id, [])
        for j, child_id in enumerate(kids):
            lines.extend(render_node(child_id, "", j == len(kids) - 1))
        if i < len(roots) - 1:
            lines.append("")
    return "\n".join(lines)


def _message_preview(content: object, max_len: int = 56) -> str:
    text = content if isinstance(content, str) else str(content)
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# ─── Banner ──────────────────────────────────────────────────────────────────


def render_banner(
    *,
    provider: str,
    model: str,
    session_id: str,
    theme: Theme | None = None,
    show_meta: bool = True,
) -> Group:
    """Build the welcome banner as a single renderable (no console I/O).

    Args:
        provider: The active provider name (``openrouter``, ``openai`` …).
        model: The full model id; the part after the last ``/`` is used
            in the status line.
        session_id: The current session id; only the first 8 chars are
            shown.
        theme: Optional :class:`Theme`. Resolved via ``load_theme()``
            when None.
        show_meta: Include the "provider/model/session" line and the
            command-hint line below the art. The classic REPL has no
            header/footer bar, so it needs this here; the full-screen
            front end shows the same info in its header instead and
            passes ``False`` to avoid showing it twice.
    """
    from phoson_cli.theme import load_theme

    theme = theme or load_theme()

    art = Text(_PHOS_ART, style=theme.art)

    # Wordmark column aligned vertically to the middle of the ASCII art.
    art_lines = _PHOS_ART.splitlines()
    mid = len(art_lines) // 2
    word_lines: list[str] = [""] * len(art_lines)
    word_lines[mid - 1] = "phoson"
    word_lines[mid] = "terminal agent"
    wordmark = Text("\n".join(word_lines))
    wordmark.highlight_words(["phoson"], style=f"bold {theme.accent}")
    wordmark.highlight_words(["terminal agent"], style=theme.muted)

    items: list[RenderableType] = [
        Text(""),
        Columns([art, wordmark], padding=(0, 4)),
        Text(""),
    ]
    if show_meta:
        short_model = model.split("/")[-1]
        items.extend(
            [
                Text(
                    f"  provider {provider}  ·  model {short_model}"
                    f"  ·  session {session_id[:8]}",
                    style=theme.muted,
                ),
                Rule(style=theme.accent_soft),
                Text(
                    "  /help for commands  ·  /sessions to resume work"
                    "  ·  /attach to add images  ·  Ctrl+T reasoning"
                    "  ·  Ctrl+C interrupt  ·  Ctrl+D exit",
                    style=theme.muted_deep,
                ),
            ]
        )
    items.append(Text(""))
    return Group(*items)


def print_banner(
    console: Console,
    *,
    provider: str,
    model: str,
    session_id: str,
    theme: Theme | None = None,
) -> None:
    """Print the welcome banner with the active provider/model/session."""
    console.print(
        render_banner(
            provider=provider, model=model, session_id=session_id, theme=theme
        )
    )
