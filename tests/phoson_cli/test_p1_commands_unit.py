"""Tests for IMPROVEMENTS.md C2 — /compact, /status and /resume commands."""

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_llm.schemas import Message
from phoson_cli.commands import COMMANDS, Command, CommandHandler
from phoson_cli.controller import SessionController
from phoson_agent.sessions.models import SessionMeta, ConversationTree

# ─── Fakes ───────────────────────────────────────────────────────────────────


class _FakeHost:
    """CommandHost double capturing printed output (pattern of B3 tests)."""

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

    def print_help(self, entries) -> None: ...

    def print_renderable(self, renderable) -> None: ...

    async def pick_model(self, models, current_model): ...
    async def pick_provider(self, providers, current_provider): ...
    async def pick_session(self, sessions, current_id): ...
    async def confirm(self, prompt: str) -> bool:
        return False

    async def run_setup(self) -> None: ...


class _FakeStorage:
    def __init__(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        self._metas = [
            SessionMeta(
                id="ccc11111-2222-3333-4444-555555555555",
                created_at=now,
                updated_at=now,
                message_count=2,
                title="first session",
            ),
            SessionMeta(
                id="ccc22222-3333-4444-5555-666666666666",
                created_at=now,
                updated_at=now,
                message_count=1,
                title="second session",
            ),
        ]

    async def list_meta(self):
        return list(self._metas)


def _make_repl() -> SimpleNamespace:
    repl = SimpleNamespace()
    repl.storage = _FakeStorage()
    repl.tree = ConversationTree.new()
    repl.current_node_id = None
    return repl


# ─── /resume ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_loads_session_by_prefix_match() -> None:
    repl = _make_repl()
    repl.load_session = AsyncMock(return_value=True)
    handler = CommandHandler(repl, host=_FakeHost())

    await handler.handle(Command(name="/resume", args="ccc11111"))

    repl.load_session.assert_awaited_once_with("ccc11111-2222-3333-4444-555555555555")


@pytest.mark.asyncio
async def test_resume_reports_ambiguous_prefixes() -> None:
    repl = _make_repl()
    repl.load_session = AsyncMock(return_value=True)
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)

    # A prefix that matches BOTH ids is ambiguous.
    await handler.handle(Command(name="/resume", args="ccc"))

    assert repl.load_session.await_count == 0
    assert any("match" in info for info in host.infos)


@pytest.mark.asyncio
async def test_resume_without_args_shows_usage() -> None:
    repl = _make_repl()
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/resume", args=""))

    assert any("Usage" in info for info in host.infos)


@pytest.mark.asyncio
async def test_resume_unknown_prefix_errors() -> None:
    repl = _make_repl()
    repl.load_session = AsyncMock(return_value=True)
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/resume", args="zzzzzzzz"))

    assert host.errors, "expected an error notice"
    repl.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_command_is_registered() -> None:
    assert "/resume" in COMMANDS


# ─── /status ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_prints_all_dimensions() -> None:
    from unittest.mock import MagicMock

    repl = _make_repl()
    repl.config = SimpleNamespace(provider="openrouter", reasoning_effort=None)
    repl.current_model = "test-model"
    repl.subagent_model = "test-model"
    repl.session_metrics = MagicMock(
        total_cost_usd=0.01,
        total_credits=0.0,
        total_input_tokens=100,
        total_output_tokens=50,
        step_count=3,
    )
    repl.engine = SimpleNamespace(_loaded_plugins=[])
    repl._context_window = 128_000
    repl._context_tokens = 500
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/status", args=""))

    output = "\n".join(host.infos)
    for expected in (
        "provider",
        "model",
        "session",
        "steps",
        "tokens",
        "cost",
        "permissions",
        "cwd",
    ):
        assert expected in output, f"/status is missing {expected!r}"


@pytest.mark.asyncio
async def test_status_command_is_registered() -> None:
    assert "/status" in COMMANDS


# ─── /compact (controller level) ────────────────────────────────────────────


class _FakeSink:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def notify(self, kind: str, message: str) -> None:
        self.notices.append((kind, message))

    def set_session(self, session_id: str) -> None: ...


def _controller_with_history(turns: int) -> SessionController:
    """A controller with a fake chat client and a populated tree."""
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        controller = SessionController.__new__(SessionController)
        # Minimal manual wiring — the engine/chat are fakes.
        controller.sink = _FakeSink()
        controller.confirmation = None
        controller.attachments = MagicMock()
        controller.attachments.__bool__ = lambda self: False
        controller.attachments.__len__ = lambda self: 0
        controller.summarizer = MagicMock()
        controller.summarizer.min_keep_messages = 4
        controller.summarizer.estimate_tokens = lambda msgs: sum(
            len(str(m.content)) for m in msgs
        )
        controller.summarizer.format_for_summary = lambda msgs: "HISTORY"
        controller.chat = MagicMock()
        from phoson_llm.schemas import LLMDoneEvent

        controller.chat.complete = AsyncMock(
            return_value=LLMDoneEvent(content="Summary of the conversation.")
        )
        controller.current_model = "test-model"
        controller.current_task = None
        controller._session = SimpleNamespace(
            tree=ConversationTree.new(),
            metrics=MagicMock(),
            current_node_id=None,
        )
        tree = controller.tree
        parent = None
        for i in range(turns):
            node = tree.append(
                parent_id=parent, message=Message(role="user", content=f"u{i} " * 20)
            )
            parent = node.id
            node = tree.append(
                parent_id=parent,
                message=Message(role="assistant", content=f"a{i} " * 40),
            )
            parent = node.id
        controller.current_node_id = parent
        return controller


@pytest.mark.asyncio
async def test_compact_creates_a_new_branch_and_moves_cursor() -> None:
    controller = _controller_with_history(turns=6)
    old_root_count = len(controller.tree.nodes)

    before, after, changed = await controller.compact_context()

    assert changed is True
    assert after < before
    # New branch appended off the root: summary message + min_keep tail.
    new_nodes = len(controller.tree.nodes) - old_root_count
    assert new_nodes == 5  # 1 summary + 4 kept (min_keep_messages)
    path = controller.tree.get_path(controller.current_node_id)
    assert len(path) == 5
    assert "[Conversation summary]" in str(path[0].content)


@pytest.mark.asyncio
async def test_compact_skips_short_conversations() -> None:
    controller = _controller_with_history(turns=1)

    before, after, changed = await controller.compact_context()

    assert changed is False
    assert before == after
    controller.chat.complete.assert_not_awaited()
    notices = dict(controller.sink.notices)
    assert any("nothing" in msg.lower() for msg in notices.values())


@pytest.mark.asyncio
async def test_compact_on_empty_session_is_a_noop() -> None:
    controller = _controller_with_history(turns=0)

    before, after, changed = await controller.compact_context()

    assert changed is False
    assert (before, after) == (0, 0)


@pytest.mark.asyncio
async def test_compact_propagates_llm_errors() -> None:
    controller = _controller_with_history(turns=6)
    controller.chat.complete = AsyncMock(side_effect=RuntimeError("provider down"))

    with pytest.raises(RuntimeError):
        await controller.compact_context()

    # The tree was not mutated.
    assert all(
        "summary" not in str(n.message.content).lower()
        for n in controller.tree.nodes.values()
    )


@pytest.mark.asyncio
async def test_compact_command_is_registered() -> None:
    assert "/compact" in COMMANDS
