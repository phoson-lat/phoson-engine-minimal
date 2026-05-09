"""Tests for the presentation helpers in :mod:`phoson_cli._views`."""

from io import StringIO

from rich.console import Console

from phoson_llm.schemas import Message
from phoson_cli._views import (
    print_banner,
    render_tree_ascii,
    _message_preview,
)
from phoson_agent.sessions.models import ConversationTree


# ─── render_tree_ascii ───────────────────────────────────────────────────────


def test_render_tree_ascii_returns_placeholder_for_empty_tree() -> None:
    tree = ConversationTree.new()
    assert render_tree_ascii(tree, current_node_id=None) == "(empty session)"


def test_render_tree_ascii_marks_current_node() -> None:
    tree = ConversationTree.new()
    root = tree.append(parent_id=None, message=Message(role="user", content="hi"))
    child = tree.append(parent_id=root.id, message=Message(role="assistant", content="hi back"))

    output = render_tree_ascii(tree, current_node_id=child.id)

    assert "← current" in output
    # Both messages should appear in some form.
    assert root.id in output
    assert child.id in output


def test_render_tree_ascii_uses_open_marker_for_current() -> None:
    tree = ConversationTree.new()
    root = tree.append(parent_id=None, message=Message(role="user", content="x"))

    out = render_tree_ascii(tree, current_node_id=root.id)
    assert out.startswith("○")  # open circle for current

    out_other = render_tree_ascii(tree, current_node_id=None)
    assert out_other.startswith("●")  # filled circle for non-current


def test_render_tree_ascii_handles_branching() -> None:
    tree = ConversationTree.new()
    root = tree.append(parent_id=None, message=Message(role="user", content="root"))
    a = tree.append(parent_id=root.id, message=Message(role="assistant", content="branch a"))
    b = tree.append(parent_id=root.id, message=Message(role="assistant", content="branch b"))

    out = render_tree_ascii(tree, current_node_id=None)

    assert "├─" in out or "└─" in out
    assert a.id in out
    assert b.id in out


# ─── _message_preview ────────────────────────────────────────────────────────


def test_message_preview_truncates_long_text() -> None:
    long_text = "x" * 100
    result = _message_preview(long_text, max_len=20)
    assert len(result) == 20
    assert result.endswith("…")


def test_message_preview_collapses_whitespace() -> None:
    assert _message_preview("a   b\n\n c") == "a b c"


def test_message_preview_handles_non_string() -> None:
    out = _message_preview(["x", "y"], max_len=50)
    assert "x" in out


# ─── print_banner ────────────────────────────────────────────────────────────


def test_print_banner_includes_provider_model_session() -> None:
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    print_banner(
        console,
        provider="openrouter",
        model="anthropic/claude-haiku-4-5",
        session_id="abcdefgh-1234-5678",
    )

    output = buf.getvalue()
    assert "openrouter" in output
    # Model is shown as the part after the last "/"
    assert "claude-haiku-4-5" in output
    # Session id is truncated to 8 chars.
    assert "abcdefgh" in output
    # Hint line shows up.
    assert "/help" in output


def test_print_banner_handles_unsegmented_model_id() -> None:
    """Models without a '/' should be displayed verbatim."""
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    print_banner(
        console,
        provider="ollama",
        model="llama3",
        session_id="zzz",
    )

    output = buf.getvalue()
    assert "ollama" in output
    assert "llama3" in output
