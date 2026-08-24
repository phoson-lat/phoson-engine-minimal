from types import SimpleNamespace

import pytest

from phoson_cli.theme import NO_COLOR
from phoson_cli.commands import Command, CommandHandler


class DummyRenderer:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class DummyRepl:
    def __init__(self) -> None:
        self.current_model = "openai/gpt-4.1-mini"
        self.subagent_model = "openai/gpt-4.1-mini"
        self.config = SimpleNamespace(
            provider="openrouter",
            model=self.current_model,
            reasoning_effort=None,
            sessions_dir="~/.phoson/sessions",
        )
        self.theme = NO_COLOR
        self.renderer = DummyRenderer()
        self.engine = SimpleNamespace(context=SimpleNamespace(extra={}))


@pytest.fixture(autouse=True)
def _no_real_save(monkeypatch):
    saved: list[object] = []
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kwargs: saved.append((config, kwargs)),
    )
    return saved


@pytest.mark.asyncio
async def test_bare_command_shows_current_value_and_usage() -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    result = await handler.handle(Command(name="/reasoning-effort", args=""))

    assert result is True
    assert "off" in repl.renderer.infos[-1]
    assert "usage" in repl.renderer.infos[-1]


@pytest.mark.asyncio
async def test_bare_command_shows_current_value_when_set() -> None:
    repl = DummyRepl()
    repl.config.reasoning_effort = "high"
    handler = CommandHandler(repl)

    await handler.handle(Command(name="/reasoning-effort", args=""))

    assert "high" in repl.renderer.infos[-1]


@pytest.mark.parametrize("value", ["low", "medium", "high"])
@pytest.mark.asyncio
async def test_sets_and_saves_a_valid_effort(value: str, _no_real_save) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    result = await handler.handle(Command(name="/reasoning-effort", args=value))

    assert result is True
    assert repl.config.reasoning_effort == value
    assert f"→ {value}" in repl.renderer.infos[-1]
    assert _no_real_save
    config, kwargs = _no_real_save[-1]
    assert kwargs == {"only_fields": {"reasoning_effort"}}


@pytest.mark.asyncio
async def test_effort_alias_works_the_same_as_full_name() -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    await handler.handle(Command(name="/effort", args="medium"))

    assert repl.config.reasoning_effort == "medium"


@pytest.mark.parametrize("off_word", ["off", "none", "default", "OFF"])
@pytest.mark.asyncio
async def test_off_clears_the_effort(off_word: str, _no_real_save) -> None:
    repl = DummyRepl()
    repl.config.reasoning_effort = "high"
    handler = CommandHandler(repl)

    result = await handler.handle(Command(name="/reasoning-effort", args=off_word))

    assert result is True
    assert repl.config.reasoning_effort is None
    assert "off" in repl.renderer.infos[-1]


@pytest.mark.asyncio
async def test_unknown_value_is_rejected_without_changing_state(_no_real_save) -> None:
    repl = DummyRepl()
    repl.config.reasoning_effort = "low"
    handler = CommandHandler(repl)

    result = await handler.handle(Command(name="/reasoning-effort", args="extreme"))

    assert result is True
    assert repl.config.reasoning_effort == "low"  # unchanged
    assert "extreme" in repl.renderer.errors[-1]
    assert not _no_real_save  # never persisted an invalid value


@pytest.mark.asyncio
async def test_value_is_case_insensitive() -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    await handler.handle(Command(name="/reasoning-effort", args="HIGH"))

    assert repl.config.reasoning_effort == "high"
