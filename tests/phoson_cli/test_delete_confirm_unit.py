"""Unit tests for B3 (IMPROVEMENTS.md): destructive deletes must confirm.

Covers the two ``CommandHandler`` delete paths that previously deleted
without asking:

- ``/delete <id>`` (``_cmd_delete``)
- the single ``d`` delete from the session picker (``_pick_session_modal``)

Both must route through ``host.confirm`` and delete nothing when the
user declines.
"""

from types import SimpleNamespace

import pytest

from phoson_cli.theme import NO_COLOR
from phoson_cli.commands import Command, CommandHandler
from phoson_cli.session_picker import SessionPickerResult


class _FakeStorage:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self._metas = self._sample_metas()

    @staticmethod
    def _sample_metas():
        import datetime

        from phoson_agent.sessions.models import SessionMeta

        now = datetime.datetime.now(datetime.UTC)
        return [
            SessionMeta(
                id="abc123456789",
                created_at=now,
                updated_at=now,
                message_count=1,
                total_cost=0.0,
                total_tokens=0,
                step_count=1,
                last_model="gpt-4o",
            )
        ]

    async def list_meta(self):
        return list(self._metas)

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)


class _FakeHost:
    """CommandHost double: records ``confirm`` calls and its own prints.

    The ``CommandHandler`` routes every ``print_*`` through the host
    (``self._r`` is the host), so the host — not the repl renderer — is
    where the user-visible output lands.
    """

    def __init__(self, confirm_result: bool = True) -> None:
        self.confirm_result = confirm_result
        self.confirm_prompts: list[str] = []
        self.pick_session_result: SessionPickerResult | None = None
        self.infos: list[str] = []
        self.errors: list[str] = []

    async def confirm(self, prompt: str) -> bool:
        self.confirm_prompts.append(prompt)
        return self.confirm_result

    async def pick_session(self, sessions, current_id: str) -> SessionPickerResult:
        assert self.pick_session_result is not None
        return self.pick_session_result

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def print_help(self, entries) -> None:
        pass


class _DummyRepl:
    def __init__(self, current_id: str = "current-id") -> None:
        self.tree = SimpleNamespace(session_id=current_id)
        self.storage = _FakeStorage()
        self.theme = NO_COLOR


@pytest.mark.asyncio
async def test_delete_command_confirms_before_deleting() -> None:
    repl = _DummyRepl()
    host = _FakeHost(confirm_result=True)
    handler = CommandHandler(repl, host)

    kept = await handler.handle(Command(name="/delete", args="abc123456789"))

    assert kept is True
    assert host.confirm_prompts == ["Delete session abc12345? This cannot be undone."]
    assert repl.storage.deleted == ["abc123456789"]
    assert any("deleted" in msg for msg in host.infos)


@pytest.mark.asyncio
async def test_delete_command_cancelled_deletes_nothing() -> None:
    repl = _DummyRepl()
    host = _FakeHost(confirm_result=False)
    handler = CommandHandler(repl, host)

    kept = await handler.handle(Command(name="/delete", args="abc123456789"))

    assert kept is True
    assert repl.storage.deleted == []
    assert host.confirm_prompts  # it did ask
    assert any("cancelled" in msg.lower() for msg in host.infos)


@pytest.mark.asyncio
async def test_delete_current_session_never_confirms() -> None:
    """The active-session guard fires before any confirmation."""
    repl = _DummyRepl(current_id="abc123456789")
    host = _FakeHost(confirm_result=True)
    handler = CommandHandler(repl, host)

    kept = await handler.handle(Command(name="/delete", args="abc123456789"))

    assert kept is True
    assert host.confirm_prompts == []
    assert repl.storage.deleted == []
    assert any("active session" in msg for msg in host.errors)


@pytest.mark.asyncio
async def test_picker_single_delete_confirms_before_deleting() -> None:
    """B3: ``d`` in the session picker must confirm before deleting."""
    repl = _DummyRepl()
    host = _FakeHost(confirm_result=True)
    host.pick_session_result = SessionPickerResult(
        session_id="abc123456789", delete=True
    )
    handler = CommandHandler(repl, host)

    kept = await handler.handle(Command(name="/sessions", args="pick"))

    assert kept is True
    assert host.confirm_prompts == ["Delete session abc12345? This cannot be undone."]
    assert repl.storage.deleted == ["abc123456789"]


@pytest.mark.asyncio
async def test_picker_single_delete_cancelled_deletes_nothing() -> None:
    repl = _DummyRepl()
    host = _FakeHost(confirm_result=False)
    host.pick_session_result = SessionPickerResult(
        session_id="abc123456789", delete=True
    )
    handler = CommandHandler(repl, host)

    await handler.handle(Command(name="/sessions", args="pick"))

    assert repl.storage.deleted == []
    assert any("cancelled" in msg.lower() for msg in host.infos)
