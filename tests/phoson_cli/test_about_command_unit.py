"""Unit tests for the /about command (T-1: on-demand banner)."""

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from phoson_cli.theme import DARK
from phoson_cli.commands import Command, CommandHandler


class _CapturingHost:
    """Presentation host that captures the renderable printed by a command."""

    def __init__(self) -> None:
        self.renderables: list[object] = []
        self.infos: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.infos.append(message)

    def print_help(self, entries) -> None:  # noqa: ARG002
        pass

    def print_renderable(self, renderable: object) -> None:
        self.renderables.append(renderable)


def _make_repl() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(provider="openrouter"),
        current_model="gpt-4o",
        tree=SimpleNamespace(session_id="abcdefgh1234"),
        theme=DARK,
    )


@pytest.mark.asyncio
async def test_about_renders_the_wordmark_and_meta() -> None:
    host = _CapturingHost()
    handler = CommandHandler(_make_repl(), host=host)

    result = await handler.handle(Command(name="/about", args=""))

    assert result is True
    assert len(host.renderables) == 1

    buf = StringIO()
    Console(file=buf, width=100, force_terminal=False, color_system=None).print(
        host.renderables[0]
    )
    text = buf.getvalue()
    # The art wordmark ...
    assert "phoson" in text
    # ... and the meta line (provider · model · session) is shown.
    assert "openrouter" in text
    assert "gpt-4o" in text
    assert "abcdefgh" in text


@pytest.mark.asyncio
async def test_about_is_registered_in_help() -> None:
    from phoson_cli.commands import COMMAND_SPECS, get_grouped_command_help

    assert any("/about" in spec.names for spec in COMMAND_SPECS)
    flat = [name for _title, rows in get_grouped_command_help() for name, _help in rows]
    assert any("/about" in name for name in flat)
