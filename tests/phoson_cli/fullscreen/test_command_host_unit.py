"""Unit tests for FullScreenCommandHost.

Drives the host against a fake app exposing ``run_float_picker``/
``run_float_confirm``/``sink``/``theme`` — no real terminal, no real
``PhosonApp`` — verifying it builds the right picker type and delegates
to the Float mechanism instead of hanging or crashing.
"""

import datetime
from types import SimpleNamespace
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


class _FakeStorage:
    """Records deletions instead of touching disk."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)


class _FakeApp:
    """Stands in for PhosonApp: records the picker and returns a canned result."""

    def __init__(
        self,
        picker_result: object = None,
        confirm_result: bool = False,
        picker_results: list | None = None,
    ) -> None:
        self.sink = _FakeSink()
        self.theme = DARK
        self.seen_pickers: list[BasePicker] = []
        self.repl = SimpleNamespace(
            storage=_FakeStorage(),
            config=SimpleNamespace(provider="openrouter"),
        )
        self._picker_results = list(picker_results) if picker_results else None

        async def _run_float_picker(picker: BasePicker) -> object:
            self.seen_pickers.append(picker)
            if self._picker_results:
                return self._picker_results.pop(0)
            return picker_result

        self.run_float_picker = AsyncMock(side_effect=_run_float_picker)
        self.run_float_confirm = AsyncMock(return_value=confirm_result)


@pytest.mark.asyncio
async def test_pick_model_opens_unified_float_picker() -> None:
    """I-113: a bare ``/model`` opens the unified multi-provider picker as
    a Float (inline autocomplete remains available when typing)."""
    picker_result = ModelPickerResult(
        model_id="claude-sonnet-4-6", provider="anthropic"
    )
    app = _FakeApp(picker_result=picker_result)
    host = FullScreenCommandHost(app)
    models = [
        ModelOption(id="gpt-4o", label="GPT-4o", provider="openai"),
        ModelOption(id="claude-sonnet-4-6", label="Sonnet", provider="anthropic"),
    ]

    result = await host.pick_model(
        models, "gpt-4o", unavailable=[("groq", "ConnectError")]
    )

    assert result == picker_result
    app.run_float_picker.assert_awaited_once()
    assert isinstance(app.seen_pickers[0], BasePicker)


@pytest.mark.asyncio
async def test_pick_model_empty_returns_cancelled_without_float() -> None:
    app = _FakeApp()
    host = FullScreenCommandHost(app)

    result = await host.pick_model([], "gpt-4o")

    assert result == ModelPickerResult(cancelled=True)
    app.run_float_picker.assert_not_called()


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


# ── B3: destructive deletes must confirm ─────────────────────────────────────


def _meta(id: str):
    from phoson_agent.sessions.models import SessionMeta

    now = datetime.datetime.now(UTC)
    return SessionMeta(
        id=id,
        created_at=now,
        updated_at=now,
        message_count=1,
        total_cost=0.0,
        total_tokens=0,
        step_count=1,
        last_model="gpt-4o",
    )


@pytest.mark.asyncio
async def test_pick_session_multi_delete_asks_and_deletes_on_confirm() -> None:
    """B3: X (delete marked) must confirm; confirming deletes and reopens."""
    app = _FakeApp(
        picker_results=[
            SessionPickerResult(delete_ids=["aaa111", "bbb222"]),
            SessionPickerResult(cancelled=True),
        ],
        confirm_result=True,
    )
    host = FullScreenCommandHost(app)

    result = await host.pick_session([_meta("aaa111"), _meta("bbb222")], "current-id")

    assert result.cancelled is True
    assert app.repl.storage.deleted == ["aaa111", "bbb222"]
    app.run_float_confirm.assert_awaited_once()
    assert "2 session(s)" in app.run_float_confirm.call_args[0][0]
    assert any(kind == "info" and "Deleted 2" in msg for kind, msg in app.sink.notices)


@pytest.mark.asyncio
async def test_pick_session_multi_delete_cancelled_deletes_nothing() -> None:
    """B3: declining the confirm must delete nothing and keep the picker open."""
    app = _FakeApp(
        picker_results=[
            SessionPickerResult(delete_ids=["aaa111"]),
            SessionPickerResult(cancelled=True),
        ],
        confirm_result=False,
    )
    host = FullScreenCommandHost(app)

    result = await host.pick_session([_meta("aaa111")], "current-id")

    assert result.cancelled is True
    assert app.repl.storage.deleted == []
    app.run_float_confirm.assert_awaited_once()
    # The picker reopened (two runs) and a cancellation notice was shown.
    assert app.run_float_picker.await_count == 2
    assert any(
        kind == "info" and "cancelled" in msg.lower() for kind, msg in app.sink.notices
    )


@pytest.mark.asyncio
async def test_pick_session_multi_delete_skips_current_session() -> None:
    """B3: the current session is never deleted, even if it slips through."""
    app = _FakeApp(
        picker_results=[
            SessionPickerResult(delete_ids=["current-id", "aaa111"]),
            SessionPickerResult(cancelled=True),
        ],
        confirm_result=True,
    )
    host = FullScreenCommandHost(app)

    await host.pick_session([_meta("current-id"), _meta("aaa111")], "current-id")

    assert app.repl.storage.deleted == ["aaa111"]
    assert "1 session(s)" in app.run_float_confirm.call_args[0][0]


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
