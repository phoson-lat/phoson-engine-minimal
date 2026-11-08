"""UX papercuts: pasted paths are messages, unknown commands suggest the
closest one, /delete matches like /resume and never fakes success,
/permissions rejects unknown tools, and error hints point at real fixes."""

from types import SimpleNamespace

import pytest

from phoson_cli.commands import Command, CommandHandler
from phoson_cli.formatting import error_hint
from phoson_cli.command_catalog import parse_command


@pytest.mark.parametrize(
    "text",
    [
        "/Users/me/app.py crashes on start",
        "/tmp/x.log has errors",
        "/ alone",
        "//escaped",
    ],
)
def test_pasted_paths_are_messages(text: str) -> None:
    assert parse_command(text) is None


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("/model gpt", "/model"),
        ("/reasoning-effort high", "/reasoning-effort"),
        ("/acme:notes x", "/acme:notes"),
    ],
)
def test_real_commands_still_parse(text: str, name: str) -> None:
    command = parse_command(text)
    assert command is not None and command.name == name


class _Host:
    def __init__(self, confirm: bool = True) -> None:
        self.info: list[str] = []
        self.errors: list[str] = []
        self._confirm = confirm

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def print_warn(self, message: str) -> None:
        self.info.append(message)

    async def confirm(self, _prompt: str) -> bool:
        return self._confirm

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Storage:
    def __init__(self, ids: list[str]) -> None:
        self.metas = [SimpleNamespace(id=i, title=f"t-{i[:4]}") for i in ids]
        self.deleted: list[str] = []

    async def list_meta(self, cwd=None):
        return list(self.metas)

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)


def _handler(storage: _Storage, active: str = "active-id", tools=("bash", "read_file")):
    handler = CommandHandler.__new__(CommandHandler)
    handler.session = SimpleNamespace(
        storage=storage,
        tree=SimpleNamespace(session_id=active),
        engine=SimpleNamespace(tools=[SimpleNamespace(name=n) for n in tools]),
    )
    handler.host = _Host()
    return handler


@pytest.mark.asyncio
async def test_delete_matches_prefixes_and_never_fakes_success() -> None:
    storage = _Storage(["42ce1111aaaa", "42cf2222bbbb", "99zz3333cccc"])
    handler = _handler(storage)

    await handler._cmd_delete(Command(name="/delete", args="nope"))
    assert "No session matching 'nope'" in handler.host.errors[-1]
    assert storage.deleted == []

    await handler._cmd_delete(Command(name="/delete", args="42c"))
    assert "2 sessions match" in handler.host.info[-4]
    assert storage.deleted == []

    await handler._cmd_delete(Command(name="/delete", args="42ce"))
    assert storage.deleted == ["42ce1111aaaa"]
    assert "42ce1111 [t-42ce] deleted" in handler.host.info[-1]


@pytest.mark.asyncio
async def test_delete_refuses_the_active_session_by_prefix() -> None:
    storage = _Storage(["active-id"])
    handler = _handler(storage, active="active-id")
    await handler._cmd_delete(Command(name="/delete", args="act"))
    assert "Cannot delete the current active session" in handler.host.errors[-1]
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_permissions_rejects_unknown_tools(monkeypatch, tmp_path) -> None:
    import phoson_cli.permissions_store as store

    monkeypatch.setattr(store, "DEFAULT_PERMISSIONS_FILE", tmp_path / "p.json")
    handler = _handler(_Storage([]))
    await handler._cmd_permissions(Command(name="/permissions", args="bsh deny"))
    assert "Unknown tool 'bsh'" in handler.host.errors[-1]
    assert "Did you mean 'bash'?" in handler.host.errors[-1]
    assert not (tmp_path / "p.json").exists()


def test_error_hints_point_at_real_fixes() -> None:
    assert "/config " not in (error_hint("max_iterations") or "")  # no such command
    assert "max_iterations" in (error_hint("max_iterations") or "")
    assert "/compact" in (error_hint("context_length_exceeded") or "")
    assert error_hint("connection_error") and error_hint("timeout")
