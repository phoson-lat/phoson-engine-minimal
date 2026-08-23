"""Unit tests for FullScreenCommandHost.

Drives the host against a fake app exposing ``run_float_picker``/
``run_float_confirm``/``sink``/``theme`` — no real terminal, no real
``PhosonApp`` — verifying it builds the right picker type and delegates
to the Float mechanism instead of hanging or crashing.
"""

import datetime
from unittest.mock import AsyncMock

import pytest

from phoson_cli.theme import DARK
from phoson_cli.models import ModelOption
from phoson_cli.pickers import BasePicker
from phoson_cli.model_picker import ModelPickerResult
from phoson_cli.session_picker import SessionPickerResult
from phoson_cli.provider_picker import ProviderPickerResult
from phoson_cli.fullscreen.command_host import FullScreenCommandHost

UTC = datetime.UTC


class _FakeSink:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def notify(self, kind: str, message: str) -> None:
        self.notices.append((kind, message))


class _FakeApp:
    """Stands in for PhosonApp: records the picker and returns a canned result."""

    def __init__(
        self, picker_result: object = None, confirm_result: bool = False
    ) -> None:
        self.sink = _FakeSink()
        self.theme = DARK
        self.seen_pickers: list[BasePicker] = []

        async def _run_float_picker(picker: BasePicker) -> object:
            self.seen_pickers.append(picker)
            return picker_result

        self.run_float_picker = AsyncMock(side_effect=_run_float_picker)
        self.run_float_confirm = AsyncMock(return_value=confirm_result)


@pytest.mark.asyncio
async def test_pick_model_points_at_inline_autocomplete_instead_of_a_float() -> None:
    """Model selection is inline autocomplete (cli_abel-style), not a

    modal — a bare ``/model`` must not open any Float; it just hints at
    typing ``/model <name>`` for the dropdown.
    """
    app = _FakeApp()
    host = FullScreenCommandHost(app)
    models = [ModelOption(id="gpt-4o", label="GPT-4o", provider="openai")]

    result = await host.pick_model(models, "gpt-4o")

    assert result == ModelPickerResult(cancelled=True)
    app.run_float_picker.assert_not_called()
    assert app.sink.notices
    assert "/model" in app.sink.notices[0][1]


@pytest.mark.asyncio
async def test_pick_provider_delegates_to_float_and_returns_its_result() -> None:
    app = _FakeApp(picker_result=ProviderPickerResult(provider="openai"))
    host = FullScreenCommandHost(app)

    result = await host.pick_provider(["openai", "anthropic"], "anthropic")

    assert result == ProviderPickerResult(provider="openai")
    app.run_float_picker.assert_awaited_once()
    assert isinstance(app.seen_pickers[0], BasePicker)


@pytest.mark.asyncio
async def test_pick_session_delegates_to_float_and_returns_its_result() -> None:
    from phoson_agent.sessions.models import SessionMeta

    now = datetime.datetime.now(UTC)
    meta = SessionMeta(
        id="abc12345",
        created_at=now,
        updated_at=now,
        message_count=2,
        total_cost=0.0,
        total_tokens=0,
        step_count=1,
        last_model="gpt-4o",
    )
    app = _FakeApp(picker_result=SessionPickerResult(session_id="abc12345"))
    host = FullScreenCommandHost(app)

    result = await host.pick_session([meta], "abc12345")

    assert result == SessionPickerResult(session_id="abc12345")
    app.run_float_picker.assert_awaited_once()
    assert isinstance(app.seen_pickers[0], BasePicker)


@pytest.mark.asyncio
async def test_confirm_delegates_to_float_confirm() -> None:
    app = _FakeApp(confirm_result=True)
    host = FullScreenCommandHost(app)

    assert await host.confirm("Really?") is True
    app.run_float_confirm.assert_awaited_once_with("Really?")


async def test_print_info_warn_error_route_to_sink_notify() -> None:
    app = _FakeApp()
    host = FullScreenCommandHost(app)

    host.print_info("hello")
    host.print_warn("careful")
    host.print_error("boom")

    assert app.sink.notices == [
        ("info", "hello"),
        ("warn", "careful"),
        ("error", "boom"),
    ]


async def test_print_help_formats_entries_as_one_notice() -> None:
    app = _FakeApp()
    host = FullScreenCommandHost(app)

    host.print_help([("/help", "Show this help"), ("/new", "Start a new session")])

    assert len(app.sink.notices) == 1
    kind, message = app.sink.notices[0]
    assert kind == "info"
    assert "/help" in message and "/new" in message


async def test_run_setup_notifies_it_is_not_available_yet() -> None:
    app = _FakeApp()
    host = FullScreenCommandHost(app)

    await host.run_setup()

    assert app.sink.notices
    assert "setup" in app.sink.notices[0][1].lower()
