"""Tests for the ``show_warnings`` switch (I-112 notice channel)."""

import warnings
from types import SimpleNamespace

import pytest

from phoson_cli import warnings_hook as wh


@pytest.fixture(autouse=True)
def _reset_hook():
    wh.set_enabled(True)
    wh.reset_notice_printer()
    yield
    wh.set_enabled(True)
    wh.reset_notice_printer()


def test_set_enabled_controls_is_enabled():
    wh.set_enabled(False)
    assert wh.is_enabled() is False
    wh.set_enabled(True)
    assert wh.is_enabled() is True


def test_showwarning_is_gated_by_enabled(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(wh, "notice_printer", calls.append)

    restore = wh.install()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            wh.set_enabled(True)
            warnings.warn("loud notice")
            assert any("loud notice" in c for c in calls)

            calls.clear()
            wh.set_enabled(False)
            warnings.warn("silent notice")
            assert calls == []
    finally:
        restore()


def test_log_handler_is_gated_by_enabled(monkeypatch):
    import logging

    calls: list[str] = []
    monkeypatch.setattr(wh, "notice_printer", calls.append)

    restore = wh.install()
    logger = logging.getLogger("phoson_cli.test_channel")
    try:
        wh.set_enabled(True)
        logger.warning("logged notice")
        assert any("logged notice" in c for c in calls)

        calls.clear()
        wh.set_enabled(False)
        logger.warning("logged silent")
        assert calls == []
    finally:
        restore()


# ─── /warnings command ───────────────────────────────────────────────────────


class DummyRenderer:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:
        self.warns.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class DummyRepl:
    def __init__(self) -> None:
        self.renderer = DummyRenderer()
        self.config = SimpleNamespace(show_warnings=True)


@pytest.fixture(autouse=True)
def _no_real_save(monkeypatch):
    saved: list[object] = []
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kwargs: saved.append((config, kwargs)),
    )
    return saved


async def test_warnings_bare_shows_status():
    from phoson_cli.commands import Command, CommandHandler

    repl = DummyRepl()
    handler = CommandHandler(repl)
    await handler.handle(Command(name="/warnings", args=""))
    assert "on" in repl.renderer.infos[-1]
    assert "usage" in repl.renderer.infos[-1]


async def test_warnings_off_toggles_and_persists(_no_real_save):
    from phoson_cli.commands import Command, CommandHandler

    repl = DummyRepl()
    handler = CommandHandler(repl)
    result = await handler.handle(Command(name="/warnings", args="off"))

    assert result is True
    assert repl.config.show_warnings is False
    assert wh.is_enabled() is False
    assert _no_real_save and _no_real_save[-1][1]["only_fields"] == {"show_warnings"}


async def test_warnings_on_re_enables():
    from phoson_cli.commands import Command, CommandHandler

    wh.set_enabled(False)
    repl = DummyRepl()
    repl.config.show_warnings = False
    handler = CommandHandler(repl)
    await handler.handle(Command(name="/warnings", args="on"))

    assert repl.config.show_warnings is True
    assert wh.is_enabled() is True


async def test_warnings_rejects_unknown_option():
    from phoson_cli.commands import Command, CommandHandler

    repl = DummyRepl()
    handler = CommandHandler(repl)
    await handler.handle(Command(name="/warnings", args="banana"))
    assert "Unknown option" in repl.renderer.errors[-1]
