"""Tests for IMPROVEMENTS.md C4 — status bar, colored tree, grouped help.

The classic-REPL /help rendering is exercised through the Renderer; the
full-screen status bar through the PhosonApp shell fixture pattern.
"""

from io import StringIO

from rich.console import Console

from phoson_cli.theme import DARK
from phoson_cli._views import render_tree_rich
from phoson_llm.schemas import Message
from phoson_cli.commands import get_grouped_command_help
from phoson_cli.renderer import Renderer
from phoson_agent.sessions.models import ConversationTree

# ─── grouped /help (commands level) ─────────────────────────────────────────


def test_grouped_help_covers_every_command() -> None:
    entries = [
        entry for _title, commands in get_grouped_command_help() for entry in commands
    ]
    flat = [name for name, _help in entries]
    # Every registered primary command appears exactly once.
    assert len(flat) == len(set(flat))
    assert "/help" in flat and "/compact" in flat and "/status" in flat


def test_grouped_help_has_expected_categories_in_order() -> None:
    grouped = get_grouped_command_help()
    titles = [title for title, _ in grouped]
    assert titles[0] == "Session"
    assert "Model" in titles
    assert "Info" in titles
    assert "Config & System" in titles
    assert "Other" not in titles  # every command is categorized today


def test_grouped_help_session_section_contains_new_commands() -> None:
    grouped = dict(get_grouped_command_help())
    session_cmds = {name for name, _ in grouped["Session"]}
    assert {"/tree", "/undo", "/compact", "/resume", "/sessions"} <= session_cmds
    # Aliased commands render as "primary · alias".
    assert any(name.startswith("/new") for name in session_cmds)


def test_renderer_print_help_renders_category_headers() -> None:
    renderer = Renderer(console=Console(file=StringIO(), width=120, highlight=False))
    with renderer.console.capture() as cap:
        renderer.print_help(get_grouped_command_help())
    output = cap.get()
    for title in ("Session", "Model", "Info", "Config & System"):
        assert title in output
    assert "/compact" in output
    assert "/resume" in output


def test_renderer_print_help_still_accepts_flat_entries() -> None:
    """Backward compatibility: a flat (name, help) list still renders."""
    renderer = Renderer(console=Console(file=StringIO(), width=120, highlight=False))
    with renderer.console.capture() as cap:
        renderer.print_help([("/old", "legacy form")])
    assert "/old" in cap.get()


# ─── colored tree ────────────────────────────────────────────────────────────


def test_render_tree_rich_empty_tree_placeholder() -> None:
    tree = ConversationTree.new()
    group = render_tree_rich(tree, None, DARK)
    console = Console(highlight=False, width=100)
    with console.capture() as cap:
        console.print(group)
    assert "(empty session)" in cap.get()


def test_render_tree_rich_marks_current_and_labels() -> None:
    tree = ConversationTree.new()
    root = tree.append(parent_id=None, message=Message(role="user", content="hello"))
    child = tree.append(
        parent_id=root.id, message=Message(role="assistant", content="world")
    )
    tree.label(child.id, "the answer")

    console = Console(highlight=False, width=120)
    with console.capture() as cap:
        console.print(render_tree_rich(tree, child.id, DARK))
    out = cap.get()

    assert "← current" in out
    assert "[the answer]" in out
    assert root.id[:8] in out
    assert child.id[:8] in out


# ─── error hints surface in the panel ───────────────────────────────────────


def test_error_panel_shows_hint_for_known_codes() -> None:
    from phoson_agent.models import AgentErrorEvent
    from phoson_cli.formatting import render_error_panel

    event = AgentErrorEvent(message="bad key", code="auth")
    console = Console(highlight=False, width=100)
    with console.capture() as cap:
        console.print(render_error_panel(event, DARK))
    out = cap.get()
    assert "hint:" in out
    assert "/setup" in out


def test_error_panel_has_no_hint_for_unknown_codes() -> None:
    from phoson_agent.models import AgentErrorEvent
    from phoson_cli.formatting import render_error_panel

    event = AgentErrorEvent(message="weird", code="mystery_code")
    console = Console(highlight=False, width=100)
    with console.capture() as cap:
        console.print(render_error_panel(event, DARK))
    assert "hint:" not in cap.get()
