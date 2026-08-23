"""Tests for the ConfirmationService classic + full-screen implementations."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.confirmation import PromptToolkitConfirmationService
from phoson_cli.ui_protocols import ConfirmationService
from phoson_cli.fullscreen.confirmation import FullScreenConfirmationService

# ── Conformance ──────────────────────────────────────────────────────────────


def test_classic_service_conforms_to_protocol() -> None:
    assert isinstance(PromptToolkitConfirmationService(), ConfirmationService)


def test_fullscreen_service_conforms_to_protocol() -> None:
    fake_app = MagicMock()
    assert isinstance(FullScreenConfirmationService(fake_app), ConfirmationService)


# ── FullScreenConfirmationService ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fullscreen_confirm_bash_delegates_to_float_with_the_command() -> None:
    fake_app = MagicMock()
    fake_app.run_float_confirm = AsyncMock(return_value=True)

    result = await FullScreenConfirmationService(fake_app).confirm_bash("rm -rf /tmp/x")

    assert result is True
    fake_app.run_float_confirm.assert_awaited_once()
    (prompt,), _ = fake_app.run_float_confirm.call_args
    assert "rm -rf /tmp/x" in prompt


@pytest.mark.asyncio
async def test_fullscreen_confirm_bash_returns_false_when_float_declines() -> None:
    fake_app = MagicMock()
    fake_app.run_float_confirm = AsyncMock(return_value=False)

    assert await FullScreenConfirmationService(fake_app).confirm_bash("ls") is False


# ── PromptToolkitConfirmationService ─────────────────────────────────────────


def _patched_session(answer: str):
    """A PromptSession whose prompt_async returns ``answer``."""
    session = MagicMock()
    session.prompt_async = AsyncMock(return_value=answer)
    return session


@pytest.mark.asyncio
async def test_confirm_bash_accepts_y_and_yes() -> None:
    for answer in ("y", "Y", "yes", "  Yes  "):
        with patch(
            "phoson_cli.confirmation.PromptSession",
            return_value=_patched_session(answer),
        ):
            assert await PromptToolkitConfirmationService().confirm_bash("ls") is True


@pytest.mark.asyncio
async def test_confirm_bash_rejects_other_input() -> None:
    for answer in ("n", "", "no", "maybe"):
        with patch(
            "phoson_cli.confirmation.PromptSession",
            return_value=_patched_session(answer),
        ):
            assert await PromptToolkitConfirmationService().confirm_bash("ls") is False


@pytest.mark.asyncio
async def test_confirm_bash_eof_or_interrupt_returns_false() -> None:
    for exc in (EOFError, KeyboardInterrupt):
        session = MagicMock()
        session.prompt_async = AsyncMock(side_effect=exc)
        with patch("phoson_cli.confirmation.PromptSession", return_value=session):
            assert await PromptToolkitConfirmationService().confirm_bash("ls") is False


# ── Controller wiring ────────────────────────────────────────────────────────


def _make_controller_with_confirmation(tmp_path, service):
    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    class _Sink:
        def set_session(self, session_id):
            pass

    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        controller = SessionController(config, _Sink(), confirmation=service)
    return controller


def test_controller_injects_confirmation_into_engine_context(tmp_path) -> None:
    service = PromptToolkitConfirmationService()
    controller = _make_controller_with_confirmation(tmp_path, service)
    assert controller.engine.context.extra["bash_confirmation"] is service


def test_controller_without_confirmation_injects_none(tmp_path) -> None:
    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    class _Sink:
        def set_session(self, session_id):
            pass

    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        controller = SessionController(config, _Sink())
    assert controller.engine.context.extra["bash_confirmation"] is None


# ── Pure formatters (phase 2A) ───────────────────────────────────────────────


def test_reasoning_panel_formatter_is_pure() -> None:
    """The shared formatter builds a renderable with no console I/O."""
    import phoson_cli.formatting as fmt

    # No console/live/thread machinery imported at module level.
    for banned in ("rich.console", "rich.live", "threading", "subprocess"):
        assert banned not in [m for m in getattr(fmt, "__dict__", {})]
    import inspect

    source = inspect.getsource(fmt)
    assert "Console(" not in source
    assert "Live(" not in source
    assert "threading" not in source

    from phoson_cli.theme import NO_COLOR

    panel = fmt.render_reasoning_panel("thoughts here", NO_COLOR)
    assert panel.title == "reasoning"


def test_renderer_delegates_to_pure_formatter() -> None:
    from rich.console import Console

    from phoson_cli.renderer import Renderer
    from phoson_cli.formatting import render_reasoning_panel

    def _text(panel) -> str:
        c = Console(record=True, width=60)
        c.print(panel)
        return c.export_text()

    r = Renderer(console=Console(record=True))
    # Instance method and the pure function render identically.
    assert _text(r.render_reasoning_panel("x")) == _text(
        render_reasoning_panel("x", r.theme)
    )
