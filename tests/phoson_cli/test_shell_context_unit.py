"""Shell-mode output shared with the agent (``phoson_cli.shell_context``)."""

import io
from unittest.mock import MagicMock, patch

from rich.console import Console

from phoson_cli.theme import DARK
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, TextBlock
from phoson_cli.controller import SessionController
from phoson_cli.formatting import ReplayToolGroup, history_blocks
from phoson_cli.shell_context import (
    SHELL_CONTEXT_HEADER,
    ShellContext,
    parse_shell_context,
    format_shell_context,
)


def test_round_trip_output() -> None:
    entry = ShellContext(command="git status\n--short", output="M a.py\n</output>")
    text = format_shell_context(entry)
    assert text.startswith(SHELL_CONTEXT_HEADER)
    assert parse_shell_context(text) == entry


def test_round_trip_error_and_empty_output() -> None:
    error = ShellContext(command="nope", output="", error="denied")
    assert parse_shell_context(format_shell_context(error)) == error
    empty = ShellContext(command="true", output="")
    assert "(no output)" in format_shell_context(empty)
    assert parse_shell_context(format_shell_context(empty)) == empty


def test_parse_ignores_other_text() -> None:
    assert parse_shell_context("hello") is None
    assert parse_shell_context(f"{SHELL_CONTEXT_HEADER} truncated") is None


def _controller(tmp_path) -> SessionController:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        return SessionController(config, MagicMock())


def test_next_message_carries_queued_shell_output(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.add_shell_context("ls", "a.py\nb.py")
    controller.add_shell_context("false", "", error="exit 1")
    assert controller.pending_shell_count == 2

    message = controller._build_user_message("what changed?")

    assert isinstance(message.content, list)
    texts = [b.text for b in message.content if isinstance(b, TextBlock)]
    assert texts[0] == "what changed?"
    assert [parse_shell_context(t) for t in texts[1:]] == [
        ShellContext(command="ls", output="a.py\nb.py"),
        ShellContext(command="false", output="", error="exit 1"),
    ]
    # Delivered once: the following message is plain again.
    assert controller.pending_shell_count == 0
    again = controller._build_user_message("again").content
    assert [b.text for b in again] == ["again"]


def test_new_session_drops_queued_shell_output(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.add_shell_context("ls", "a.py")
    controller._reset_session()
    assert controller.pending_shell_count == 0


def test_replay_renders_shell_blocks_as_bash_cards() -> None:
    shell = format_shell_context(ShellContext(command="ls", output="a.py"))
    messages = [
        Message(
            role="user",
            content=[TextBlock(text="look at this"), TextBlock(text=shell)],
        )
    ]

    items = history_blocks(messages, DARK)

    groups = [item for item in items if isinstance(item, ReplayToolGroup)]
    assert len(groups) == 1
    event, args = groups[0].calls[0]
    assert event.tool_name == "bash"
    assert event.result == "a.py"
    assert args == {"command": "ls"}
    # The user's message line shows only what they typed.
    console = Console(width=100, record=True, file=io.StringIO())
    for item in items:
        if not isinstance(item, ReplayToolGroup):
            console.print(item)
    rendered = console.export_text()
    assert "look at this" in rendered
    assert SHELL_CONTEXT_HEADER not in rendered
