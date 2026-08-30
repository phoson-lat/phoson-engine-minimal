"""Unit tests for plugin-contributed slash commands (I-110)."""

from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document

from phoson_agent import Plugin, CliCommandSpec
from phoson_cli.commands import (
    Command,
    CommandHandler,
    SlashCompleter,
    build_command_catalog,
    get_grouped_command_help,
)


class _Host:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:  # pragma: no cover - protocol stub
        pass

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class _Plugin(Plugin):
    @property
    def name(self) -> str:
        return "demo"

    def get_commands(self) -> list[CliCommandSpec]:
        return [
            CliCommandSpec(
                names=("/demo", "/d"),
                help="Run the demo plugin",
                handler="handle_demo",
                category="Demo",
            )
        ]

    async def handle_demo(self, command, context) -> bool:  # noqa: ANN001
        context.notify("info", f"{command.name}: {command.args}")
        return True


def _repl(catalog):  # noqa: ANN001
    return SimpleNamespace(
        tree=SimpleNamespace(session_id="session-123"),
        _controller=SimpleNamespace(command_catalog=catalog),
    )


def test_catalog_adds_plugin_specs_to_help_under_declared_category() -> None:
    catalog = build_command_catalog([_Plugin()], version=4)
    grouped = dict(get_grouped_command_help(catalog.specs))

    assert catalog.version == 4
    assert grouped["Demo"] == [("/demo · /d", "Run the demo plugin")]


@pytest.mark.asyncio
async def test_handler_dispatches_to_loaded_plugin_instance() -> None:
    catalog = build_command_catalog([_Plugin()], version=1)
    host = _Host()
    handler = CommandHandler(_repl(catalog), host=host)

    assert await handler.handle(Command(name="/demo", args="hello")) is True
    assert host.infos == ["/demo: hello"]


def test_slash_completer_reads_the_live_catalog_on_every_pass() -> None:
    current = build_command_catalog([])
    completer = SlashCompleter(lambda: current)
    document = Document("/demo", cursor_position=5)
    assert list(completer.get_completions(document, object())) == []

    current = build_command_catalog([_Plugin()], version=1)
    completions = list(completer.get_completions(document, object()))

    assert [completion.text for completion in completions] == ["/demo"]
    assert "Run the demo plugin" in str(completions[0].display_meta)


@pytest.mark.parametrize(
    "plugin, message",
    [
        (
            type(
                "BadNamePlugin",
                (Plugin,),
                {
                    "name": property(lambda self: "bad-name"),
                    "get_commands": lambda self: [
                        CliCommandSpec(("not-slash",), "bad", "handle")
                    ],
                    "handle": lambda self: None,
                },
            )(),
            "slash commands",
        ),
        (
            type(
                "CollisionPlugin",
                (Plugin,),
                {
                    "name": property(lambda self: "collision"),
                    "get_commands": lambda self: [
                        CliCommandSpec(("/help",), "bad", "handle")
                    ],
                    "handle": lambda self: None,
                },
            )(),
            "conflicts with a native command",
        ),
        (
            type(
                "SyncHandlerPlugin",
                (Plugin,),
                {
                    "name": property(lambda self: "sync"),
                    "get_commands": lambda self: [
                        CliCommandSpec(("/sync",), "bad", "handle")
                    ],
                    "handle": lambda self: None,
                },
            )(),
            "must be an async method",
        ),
    ],
)
def test_catalog_rejects_invalid_or_conflicting_plugin_commands(
    plugin, message
) -> None:
    with pytest.raises(ValueError, match=message):
        build_command_catalog([plugin])


def test_catalog_rejects_collision_between_plugins() -> None:
    class OtherPlugin(_Plugin):
        @property
        def name(self) -> str:
            return "other"

    with pytest.raises(ValueError, match="conflicts with another plugin"):
        build_command_catalog([_Plugin(), OtherPlugin()])
