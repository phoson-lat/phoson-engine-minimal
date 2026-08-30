"""Tests for plugin-contributed per-session themes (I-110)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from phoson_agent import Plugin, ThemeExtension
from phoson_cli.theme import get_theme, load_theme, build_theme_registry
from phoson_cli.commands import Command, CommandHandler


class _ThemePlugin(Plugin):
    @property
    def name(self) -> str:
        return "example-theme"

    def get_theme_extension(self) -> ThemeExtension:
        return ThemeExtension(
            name="example-neon",
            description="cyan example theme",
            tokens={"accent": "cyan", "pt_accent": "cyan"},
        )


def test_plugin_theme_derives_from_base_and_resolves_from_config(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    registry = build_theme_registry([_ThemePlugin()])

    theme = load_theme("example-neon", registry=registry)

    assert theme.name == "example-neon"
    assert theme.accent == "cyan"
    assert theme.text == "white"  # inherited from dark
    assert registry.rows()[-1] == ("example-neon", "cyan example theme")
    assert get_theme("example-neon", registry=registry) is theme


def test_plugin_theme_remains_overridden_by_no_color(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    registry = build_theme_registry([_ThemePlugin()])

    assert load_theme("example-neon", registry=registry).name == "no-color"


def test_theme_picker_includes_registered_plugin_theme() -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("dark", registry=build_theme_registry([_ThemePlugin()]))

    text = "".join(piece for _style, piece in picker._render())
    assert "example-neon" in text
    assert "cyan example theme" in text


@pytest.mark.asyncio
async def test_theme_command_lists_and_selects_registered_theme() -> None:
    registry = build_theme_registry([_ThemePlugin()])
    infos: list[str] = []
    applied: list[str] = []
    repl = SimpleNamespace(
        theme=SimpleNamespace(name="dark"),
        theme_registry=registry,
        config=SimpleNamespace(theme="dark"),
    )
    host = SimpleNamespace(
        print_info=infos.append,
        print_error=lambda message: pytest.fail(message),
        apply_theme=lambda theme: applied.append(theme.name),
        pick_theme=AsyncMock(),
    )
    handler = CommandHandler(repl, host=host)

    with patch("phoson_cli.commands.save_config"):
        assert await handler.handle(Command("/theme", "list")) is True
        assert await handler.handle(Command("/theme", "example-neon")) is True

    assert any("example-neon" in line for line in infos)
    assert repl.config.theme == "example-neon"
    assert applied == ["example-neon"]


@pytest.mark.parametrize(
    "extension, message",
    [
        (ThemeExtension("dark", "conflict"), "conflicts"),
        (ThemeExtension("bad", "bad", tokens={"unknown": "red"}), "unknown tokens"),
    ],
)
def test_theme_registry_rejects_invalid_plugin_extensions(extension, message) -> None:
    class BadPlugin(Plugin):
        @property
        def name(self) -> str:
            return "bad-theme"

        def get_theme_extension(self) -> ThemeExtension:
            return extension

    with pytest.raises(ValueError, match=message):
        build_theme_registry([BadPlugin()])
